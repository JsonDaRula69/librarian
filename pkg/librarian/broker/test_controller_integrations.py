import importlib
import subprocess
from pathlib import Path

import pytest


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args]).decode().strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'upstream'
    repo.mkdir()
    git(repo, 'init', '-q')
    for name, data in {'Documentation/index.rst': b'Canonical docs',
                       'Documentation/schema.yaml': b'type: object',
                       'Documentation/logo.bin': b'\x00\xff',
                       'kernel/main.c': b'code', 'README': b'read me'}.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
        'commit', '-qm', 'fixture')
    return repo, git(repo, 'rev-parse', 'HEAD')


def test_canonical_boundary_includes_assets_not_kernel(tmp_path, repository):
    module = importlib.import_module('broker.controller_integrations')
    repo, commit = repository
    controller = module.IntegratedController('run', tmp_path / 'corpus', {
        'kernel': {'kind': 'git', 'repository': str(repo), 'commit': commit,
                   'documentation_roots': ['Documentation']} })
    receipt = controller.acquire('kernel')
    assert receipt['status'] == 'complete', receipt
    assert {row['path'] for row in receipt['occurrences']} == {
        'Documentation/index.rst', 'Documentation/schema.yaml', 'Documentation/logo.bin'}
    assert receipt['documentation_roots'] == ['Documentation']
    assert controller.evidence().snapshot()['sources']['kernel'] == receipt


def test_remote_pin_and_provenance(tmp_path, repository):
    from broker.controller_integrations import IntegratedController
    from contextlib import contextmanager
    repo, pin = repository
    class FixtureRemote:
        @contextmanager
        def acquire(self, url, commit):
            assert url == 'https://example.com/project.git'
            assert commit == pin
            yield repo
    controller = IntegratedController('remote', tmp_path / 'corpus', {
        'docs': {'kind': 'git', 'repository': 'https://example.com/project.git',
                 'commit': pin, 'documentation_roots': ['Documentation']}},
        remote=FixtureRemote())
    result = controller.acquire('docs')
    assert result['status'] == 'complete', result
    assert result['occurrences'][0]['repository_ref'].startswith('https://example.com/project.git@')


def test_remote_git_fetch_real_local_fixture(tmp_path, repository):
    from broker.controller_integrations import GitRemote
    repo, pin = repository
    # Only the protected transport seam changes: exercise real Git fetch, no network.
    class LocalFixture(GitRemote):
        def _validated_url(self, url):
            return str(repo)
        protocols = 'file'
    with LocalFixture().acquire('https://example.com/project.git', pin) as fetched:
        assert git(fetched, 'rev-parse', 'FETCH_HEAD') == pin
        assert not (fetched / 'Documentation').exists()


@pytest.mark.parametrize('url', ['file:///tmp/repo', 'http://example.com/a',
    'ssh://example.com/a', 'https://user@example.com/a', 'https://example.com/a?x=1',
    'https://example.com/../a', 'https://127.0.0.1/a', 'https://example.com/a%2fb'])
def test_remote_url_rejection(url):
    from broker.controller_integrations import GitRemote
    from broker.acquisition_controller import BoundaryError
    with pytest.raises(BoundaryError):
        GitRemote()._validated_url(url)


class FakeUpstream:
    def __init__(self):
        self.requests = []
        self.status = 'completed'
    def __call__(self, request):
        from broker.proxy import UpstreamResponse
        import json
        self.requests.append(request)
        if request.method == 'POST':
            return UpstreamResponse(200, json.dumps({'operation_ids': ['op_' + str(len(self.requests))]}).encode())
        return UpstreamResponse(200, json.dumps({'status': self.status}).encode())


def test_submit_retains_controller_provenance_metadata(ingestion_factory):
    import json
    from email.parser import BytesParser
    from email.policy import default
    from broker.proxy import UpstreamResponse
    from broker.controller_integrations import DOC_RETAIN_STRATEGY

    requests = []
    def upstream(request):
        requests.append(request)
        return UpstreamResponse(202, b'{"operation_ids":["op-1"],"document_ids":["doc-1"]}')

    ingestion = ingestion_factory(upstream, acquire=True, caller='trusted-caller')
    ingestion.controller._state['sources']['docs']['product_id'] = 'widget'
    ingestion.controller._state['sources']['docs']['version'] = '1.2'
    artifact = ingestion.prepare('docs')[0]
    receipt = ingestion.submit(artifact['id'])

    request = requests[0]
    content_type = request.content_type
    raw = (b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + request.body)
    part = BytesParser(policy=default).parsebytes(raw).get_payload(0)
    payload = json.loads(part.get_payload(decode=True).decode())
    context = json.loads(payload['files_metadata'][0]['context'])
    assert context['product'] == 'widget'
    assert context['version'] == '1.2'
    assert context['caller'] == 'trusted-caller'
    assert context['bank'] == 'Liv'
    assert context['artifact_sha256'] == artifact['sha256']
    assert context['source_sha256'] == [artifact['provenance'][0]['sha256']]
    assert receipt['context'] == context
    assert receipt['document_ids'] == ['doc-1']
    # Docs must ride the document-ingestion strategy, not the bank default.
    assert payload['files_metadata'][0]['strategy'] == DOC_RETAIN_STRATEGY == 'Doc Ingestion'
    # Tag scheme v2: dimensions plus attributes, and no legacy hub tag.
    tags = payload['files_metadata'][0]['tags']
    assert tags[0] == 'corpus:widget' and tags[1] == 'doctype:documentation'
    assert 'documentation' not in tags and len(tags) <= 6


def test_provenance_changes_do_not_change_deduped_artifact_id(ingestion_factory):
    import copy
    upstream = FakeUpstream()
    ingestion = ingestion_factory(upstream, acquire=True)
    first = ingestion.prepare('docs')[0]
    source = ingestion.controller._state['sources']['docs']
    changed = copy.deepcopy(source['occurrences'][0])
    changed['id'] = 'new-controller-alias'
    changed['repository_ref'] = 'different-approved-ref'
    source['occurrences'].append(changed)
    second = ingestion.prepare('docs')[0]
    assert second['id'] == first['id']
    assert changed['id'] in second['occurrence_ids']


def test_ingestion_only_controller_artifacts_and_verified_receipts(tmp_path, repository):
    from broker.controller_integrations import IntegratedController, ControllerIngestion
    from broker.acquisition_controller import BoundaryError
    repo, pin = repository
    controller = IntegratedController('run', tmp_path / 'corpus', {'docs': {
        'kind': 'git', 'repository': str(repo), 'commit': pin,
        'documentation_roots': ['Documentation']}})
    assert controller.acquire('docs')['status'] == 'complete'
    upstream = FakeUpstream()
    ingestion = ControllerIngestion(controller, 'Liv', upstream)
    artifacts = ingestion.prepare('docs')
    assert len(artifacts) == 2
    asset = next(row for row in controller.evidence().snapshot()['sources']['docs']['occurrences']
                 if row['path'].endswith('logo.bin'))
    classification = ingestion.state['classifications'][asset['id']]
    assert classification['classification'] == 'asset-only'
    assert classification['sha256'] == asset['sha256']
    with pytest.raises(BoundaryError):
        ingestion.submit('../agent.md')
    assert upstream.requests == []
    receipt = ingestion.submit(artifacts[0]['id'])
    assert receipt['status'] == 'accepted'
    assert receipt['bank'] == 'Liv'
    assert receipt['sha256'] == artifacts[0]['sha256']
    assert upstream.requests[0].path == '/v1/default/banks/Liv/files/retain'
    assert ingestion.poll(artifacts[0]['id'])['status'] == 'completed'
    assert upstream.requests[-1].method == 'GET'
    assert ingestion.submit(artifacts[0]['id'])['status'] == 'completed'
    assert len(upstream.requests) == 2
    (controller.root / artifacts[1]['path']).write_bytes(b'agent mutation')
    with pytest.raises(BoundaryError, match='hash'):
        ingestion.submit(artifacts[1]['id'])
    assert len(upstream.requests) == 2
    state = controller.evidence().snapshot()
    assert state['ingestion']['bank'] == 'Liv'
    assert state['ingestion']['receipts'][artifacts[0]['id']]['status'] == 'completed'


@pytest.mark.parametrize('change', [
    {'documentation_roots': []}, {'documentation_roots': ['missing']},
    {'documentation_roots': ['../kernel']}, {'documentation_roots': ['Documentation'], 'paths': ['index.rst']},
    {'commit': 'HEAD'}, {'commit': '0' * 40}])
def test_invalid_plan_blocks(tmp_path, repository, change):
    from broker.controller_integrations import IntegratedController
    repo, pin = repository
    plan = dict(kind='git', repository=str(repo), commit=pin, documentation_roots=['Documentation'])
    plan.update(change)
    c = IntegratedController('run', tmp_path / 'corpus', {'docs': plan})
    assert c.acquire('docs')['status'] == 'blocked'


def test_total_blob_budget(tmp_path, repository):
    from broker.controller_integrations import IntegratedController
    repo, pin = repository
    c = IntegratedController('run', tmp_path / 'corpus', {'docs': dict(kind='git',
        repository=str(repo), commit=pin, documentation_roots=['Documentation'])}, max_total_bytes=1)
    assert c.acquire('docs')['status'] != 'complete'


def test_receipt_verifier_fails_until_all_completed(tmp_path, repository):
    from broker.controller_integrations import IntegratedController, ControllerIngestion, validate_ingestion
    repo, pin = repository
    c = IntegratedController('run', tmp_path / 'corpus', {'docs': dict(kind='git',
        repository=str(repo), commit=pin, documentation_roots=['Documentation'])})
    c.acquire('docs')
    upstream = FakeUpstream()
    ingestion = ControllerIngestion(c, 'Liv', upstream)
    artifacts = ingestion.prepare('docs')
    contract = dict(run_id='run', corpus_root=str(c.root), hindsight_bank='Liv')
    assert validate_ingestion(c.evidence(), contract, artifacts)
    for artifact in artifacts:
        ingestion.submit(artifact['id'])
        ingestion.poll(artifact['id'])
    assert validate_ingestion(c.evidence(), contract, artifacts) == []
    assert validate_ingestion(c.evidence(), dict(contract, hindsight_bank='Other'), artifacts)
    assert validate_ingestion(c.evidence(), contract, artifacts[:-1])
    assert validate_ingestion(c.evidence().snapshot(), contract, artifacts)
    (c.root / artifacts[0]['path']).write_text('changed')
    assert validate_ingestion(c.evidence(), contract, artifacts)


@pytest.mark.parametrize('mode', ['budget', 'wrong-pin'])
def test_remote_fetch_failures_cleanup(tmp_path, repository, mode):
    from broker.controller_integrations import GitRemote
    from broker.acquisition_controller import BoundaryError
    repo, pin = repository
    class LocalFixture(GitRemote):
        protocols = 'file'
        def _validated_url(self, url):
            return str(repo)
    remote = LocalFixture(max_repository_bytes=1 if mode == 'budget' else 1000000)
    with pytest.raises(BoundaryError):
        with remote.acquire('https://example.com/project.git', pin if mode == 'budget' else '0' * 40):
            pytest.fail('failed acquisition yielded repository')


def test_no_checkout_filters_hooks_or_symlink_dereference(tmp_path, repository):
    from broker.controller_integrations import IntegratedController
    repo, _ = repository
    marker = tmp_path / 'executed'
    (repo / '.git' / 'hooks' / 'post-checkout').write_text('#!/bin/sh\ntouch ' + str(marker))
    (repo / '.git' / 'hooks' / 'post-checkout').chmod(0o755)
    git(repo, 'config', 'filter.evil.smudge', 'touch ' + str(marker))
    (repo / '.gitattributes').write_text('* filter=evil')
    (repo / 'Documentation' / 'z-link').symlink_to('/etc/passwd')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=T', '-c', 'user.email=t@example.test', 'commit', '-qm', 'unsafe tree')
    pin = git(repo, 'rev-parse', 'HEAD')
    c = IntegratedController('run', tmp_path / 'corpus', {'docs': dict(kind='git',
        repository=str(repo), commit=pin, documentation_roots=['Documentation'])})
    result = c.acquire('docs')
    assert result['status'] != 'complete'
    assert 'symlink' in result['error']
    assert not marker.exists()


@pytest.mark.parametrize('response_mode', ['nonjson', 'http-error', 'duplicate', 'timeout'])
def test_unverified_ingestion_never_completes_or_reposts(tmp_path, repository, response_mode):
    from broker.controller_integrations import IntegratedController, ControllerIngestion
    from broker.proxy import UpstreamResponse
    repo, pin = repository
    c = IntegratedController('run', tmp_path / 'corpus', {'docs': dict(kind='git',
        repository=str(repo), commit=pin, documentation_roots=['Documentation'])})
    c.acquire('docs')
    calls = []
    def transport(request):
        calls.append(request)
        if response_mode == 'timeout':
            raise TimeoutError()
        if response_mode == 'nonjson':
            return UpstreamResponse(200, b'not json')
        if response_mode == 'http-error':
            return UpstreamResponse(500, b'{"operation_ids": ["op"]}')
        return UpstreamResponse(200, b'{"operation_ids": ["op"]}')
    ingestion = ControllerIngestion(c, 'Liv', transport)
    artifacts = ingestion.prepare('docs')
    first = ingestion.submit(artifacts[0]['id'])
    target = artifacts[0]['id']
    if response_mode == 'duplicate':
        assert first['status'] == 'accepted'
        target = artifacts[1]['id']
        assert ingestion.submit(target)['status'] == 'unknown'
    else:
        assert first['status'] == 'unknown'
    count = len(calls)
    assert ingestion.submit(target)['status'] == 'unknown'
    assert len(calls) == count


def test_pending_polls_are_bounded(tmp_path, repository):
    from broker.controller_integrations import IntegratedController, ControllerIngestion
    from broker.acquisition_controller import BoundaryError
    repo, pin = repository
    c = IntegratedController('run', tmp_path / 'corpus', {'docs': dict(kind='git',
        repository=str(repo), commit=pin, documentation_roots=['Documentation'])})
    c.acquire('docs')
    upstream = FakeUpstream()
    upstream.status = 'pending'
    ingestion = ControllerIngestion(c, 'Liv', upstream, max_polls=1)
    artifact = ingestion.prepare('docs')[0]
    ingestion.submit(artifact['id'])
    assert ingestion.poll(artifact['id'])['status'] == 'pending'
    with pytest.raises(BoundaryError, match='budget'):
        ingestion.poll(artifact['id'])


@pytest.fixture
def ingestion_factory(tmp_path, repository):
    from broker.controller_integrations import IntegratedController, ControllerIngestion
    repo, pin = repository
    plan = {'docs': dict(kind='git', repository=str(repo), commit=pin,
                         documentation_roots=['Documentation'])}
    def make(upstream, run='run', acquire=False, **kwargs):
        controller = IntegratedController(run, tmp_path / 'corpus', plan)
        if acquire:
            assert controller.acquire('docs')['status'] == 'complete'
        return ControllerIngestion(controller, 'Liv', upstream, **kwargs)
    return make


def test_restart_recovers_accepted_without_repost(ingestion_factory):
    upstream = FakeUpstream()
    first = ingestion_factory(upstream, acquire=True)
    artifact = first.prepare('docs')[0]
    accepted = first.submit(artifact['id'])
    restarted = ingestion_factory(upstream)
    assert restarted.submit(artifact['id']) == accepted
    assert restarted.poll(artifact['id'])['status'] == 'completed'
    assert [r.method for r in upstream.requests] == ['POST', 'GET']



def test_content_identity_many_to_one_provenance(ingestion_factory):
    import copy
    upstream = FakeUpstream()
    ingestion = ingestion_factory(upstream, acquire=True)
    source = ingestion.controller._state['sources']['docs']
    alias = copy.deepcopy(source['occurrences'][0])
    alias['id'] = 'another:location'
    source['occurrences'].append(alias)
    artifacts = ingestion.prepare('docs')
    assert len(artifacts) == 2
    artifact = next(a for a in artifacts if alias['id'] in a['occurrence_ids'])
    assert len(artifact['occurrence_ids']) == 2
    assert alias['id'].encode() not in (ingestion.controller.root / artifact['path']).read_bytes()
    assert artifact['id'] == artifact['sha256']
    restarted = ingestion_factory(upstream)
    assert restarted.state['artifacts'][artifact['id']] == artifact



def test_controller_batch_resume_after_poll_budget(ingestion_factory):
    upstream = FakeUpstream()
    upstream.status = 'pending'
    ingestion = ingestion_factory(upstream, acquire=True, max_polls=1)
    ingestion.prepare('docs')
    result = ingestion.resume(max_submissions=2, max_status_checks=1)
    assert result['submitted'] == 2
    assert result['checked'] == 1
    assert result['has_more']
    restarted = ingestion_factory(upstream, max_polls=1)
    upstream.status = 'completed'
    result = restarted.resume(max_submissions=2, max_status_checks=3)
    assert result['submitted'] == 0
    assert result['checked'] == 2
    assert not result['has_more']
    assert len([r for r in upstream.requests if r.method == 'POST']) == 2


def test_interrupted_post_claim_survives_restart(ingestion_factory):
    calls = []
    def interrupted(request):
        calls.append(request)
        raise KeyboardInterrupt()
    ingestion = ingestion_factory(interrupted, acquire=True)
    artifact = ingestion.prepare('docs')[0]
    with pytest.raises(KeyboardInterrupt):
        ingestion.submit(artifact['id'])
    restarted = ingestion_factory(interrupted)
    assert restarted.submit(artifact['id'])['status'] == 'unknown'
    assert len(calls) == 1


def test_claim_shared_across_runs_not_callers(ingestion_factory):
    import copy
    upstream = FakeUpstream()
    first = ingestion_factory(upstream, acquire=True, caller='liv')
    artifacts = first.prepare('docs')
    accepted = first.submit(artifacts[0]['id'])
    second = ingestion_factory(upstream, run='second', caller='liv')
    second.controller._state['sources'] = copy.deepcopy(first.controller._state['sources'])
    assert second.prepare('docs') == artifacts
    receipt = second.submit(artifacts[0]['id'])
    assert receipt['operation_id'] == accepted['operation_id']
    assert receipt['run_id'] == 'second'
    other = ingestion_factory(upstream, caller='other')
    assert other.state['artifacts'] == {}
    from broker.acquisition_controller import BoundaryError
    with pytest.raises(BoundaryError):
        other.submit(artifacts[0]['id'])
    assert len(upstream.requests) == 1



def test_restart_preserves_completion_provenance(ingestion_factory):
    from broker.controller_integrations import validate_ingestion
    upstream = FakeUpstream()
    first = ingestion_factory(upstream, acquire=True)
    artifacts = first.prepare('docs')
    first.resume(max_status_checks=3)
    restarted = ingestion_factory(upstream)
    contract = dict(run_id='run', corpus_root=str(restarted.controller.root), hindsight_bank='Liv')
    assert validate_ingestion(restarted.controller.evidence(), contract, artifacts) == []


def test_cross_run_completed_claim_not_repolled(ingestion_factory):
    import copy
    upstream = FakeUpstream()
    first = ingestion_factory(upstream, acquire=True)
    artifact = first.prepare('docs')[0]
    first.submit(artifact['id'])
    second = ingestion_factory(upstream, run='second')
    second.controller._state['sources'] = copy.deepcopy(first.controller._state['sources'])
    second.prepare('docs')
    second.submit(artifact['id'])
    second.poll(artifact['id'])
    count = len(upstream.requests)
    assert first.poll(artifact['id'])['status'] == 'completed'
    assert len(upstream.requests) == count



def test_trusted_discovery_can_extend_plan(ingestion_factory):
    import copy
    first = ingestion_factory(FakeUpstream(), acquire=True)
    first.controller.plan['new-docs'] = copy.deepcopy(first.controller.plan['docs'])
    assert len(first.prepare('docs')) == 2



def test_ledger_is_private(ingestion_factory):
    ingestion = ingestion_factory(FakeUpstream())
    assert ingestion.ledger_path.stat().st_mode & 0o077 == 0
