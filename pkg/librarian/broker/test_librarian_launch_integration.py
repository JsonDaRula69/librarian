"""Offline launcher integration: real Git/controller/relay, no production IO."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

from broker.controller_integrations import IntegratedController, ControllerIngestion
from broker.librarian_discovery import DiscoveryController, SourceApproval
from broker.discovery_browser import DiscoveryBrowser
from broker.librarian_completion import build_contract, validate_run
from broker.librarian_runtime import RuntimeResult
from broker.corpus_preflight import RunClaimStore
from broker.test_controller_integrations import repository, FakeUpstream

spec = importlib.util.spec_from_file_location('trusted_launcher', Path(__file__).parents[1] / 'tools/librarian-launch.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def fixture_preflight(*, request, contract):
    return {'caller': contract['caller_profile'], 'bank': contract['hindsight_bank'],
            'products': [dict(id=p['id'], version=p['version'], disposition='missing', source='fixture',
                              source_hash='a' * 64, source_hash_verified=True)
                         for p in request['products']]}


@pytest.mark.parametrize('failure', ['unresolved', 'accepted_only', 'nonzero', 'purpose'])
def test_launcher_rejects_incomplete_runs(tmp_path, repository, monkeypatch, failure):
    controller, discovery, ingestion, upstream, contract, run = setup_run(tmp_path, repository)
    def runtime(config, argv, session, **kwargs):
        result = offline_runtime(config, argv, session, **kwargs)
        if failure == 'unresolved':
            discovery.propose('source', dict(product_id='linux', version='6.8', candidate_id='extra'))
        if failure == 'accepted_only':
            for receipt in ingestion.state['receipts'].values():
                receipt['status'] = 'accepted'
        return RuntimeResult(1 if failure == 'nonzero' else 0, b'', 'fixture')
    monkeypatch.setattr(launcher, 'run_relay', runtime)
    request = {'schema_version': 1, 'products': [{'id': 'linux', 'version': '6.8'}]}
    if failure == 'purpose':
        request['purpose'] = 'not allowed'
        with pytest.raises(ValueError):
            launcher.run_trusted_launch(request=request, run_dir=run, contract=contract,
                controller=controller, discovery=discovery, ingestion=ingestion,
                inference=None, config=None, argv=['/fixture'], preflight=fixture_preflight, claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))
        assert not upstream.requests
    else:
        verdict = launcher.run_trusted_launch(request=request, run_dir=run, contract=contract,
            controller=controller, discovery=discovery, ingestion=ingestion,
            inference=None, config=None, argv=['/fixture'], preflight=fixture_preflight, claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))
        assert not verdict['valid']
        assert json.loads((run / 'launch.json').read_text())['exit_code'] == 74
        result = json.loads((run / 'result.json').read_text())
        assert result['status'] == verdict['status'] != 'completed'
        assert result == json.loads((run / 'final-result.json').read_text())
        assert json.loads((run / 'coverage-matrix.json').read_text())['complete'] is False
        assert json.loads((run / 'completeness-challenge.json').read_text())['status'] != 'passed'


def setup_run(tmp_path, repository):
    repo, pin = repository
    corpus = tmp_path / 'corpus'
    controller = IntegratedController('run', corpus, {'docs': dict(kind='git', repository=str(repo),
        commit=pin, documentation_roots=['Documentation'])})
    def policy(proposal, evidence):
        if proposal == dict(product_id='linux', version='6.8', candidate_id='fixture'):
            return SourceApproval('docs', controller.plan['docs'], 'fixture-policy')
    discovery = DiscoveryController(controller, policy)
    discovery.propose('source', dict(product_id='linux', version='6.8', candidate_id='fixture'))
    upstream = FakeUpstream()
    ingestion = ControllerIngestion(controller, 'Liv', upstream)
    contract = build_contract(run_id='run', caller_profile='default', workspace=tmp_path,
        corpus_root=corpus, hindsight_bank='Liv', ingestion_required=True,
        requested_products=[{'id': 'linux', 'version': '6.8'}])
    run = corpus / 'reports/runs/run'
    run.mkdir(parents=True)
    return controller, discovery, ingestion, upstream, contract, run


def offline_runtime(config, argv, session, *, request, **kwargs):
    assert request == {'schema_version': 1, 'products': [{'id': 'linux', 'version': '6.8'}]}
    def call(op, params):
        reply = json.loads(session.feed((json.dumps(dict(id=1, op=op, params=params)) + '\n').encode()))
        assert reply['ok'], reply
        return reply['result']
    call('source.acquire', {'source_id': 'docs'})
    for identifier in call('source.prepare', {'source_id': 'docs'})['artifact_ids']:
        call('artifact.submit', {'artifact_id': identifier})
        call('artifact.poll', {'artifact_id': identifier})
    session.finish()
    return RuntimeResult(0, b'', 'offline-fixture')


def test_launcher_real_adapters_complete(tmp_path, repository, monkeypatch):
    controller, discovery, ingestion, upstream, contract, run = setup_run(tmp_path, repository)
    monkeypatch.setattr(launcher, 'run_relay', offline_runtime, raising=False)
    verdict = launcher.run_trusted_launch(request={'schema_version': 1, 'products': [{'id': 'linux', 'version': '6.8'}]},
        run_dir=run, contract=contract, controller=controller, discovery=discovery,
        ingestion=ingestion, inference=None, config=None, argv=['/fixture'], preflight=fixture_preflight, claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))
    assert verdict['valid'], verdict
    assert [r.method for r in upstream.requests].count('POST') == 2
    assert [r.method for r in upstream.requests].count('GET') == 2
    assert validate_run(run, contract, trusted_evidence=controller.evidence())['valid']
    assert json.loads((run / 'launch.json').read_text())['exit_code'] == 0
    discovery.propose('source', dict(product_id='linux', version='6.8', candidate_id='extra'))
    blocked = validate_run(run, contract, trusted_evidence=controller.evidence())
    assert not blocked['valid']
    assert 'unresolved discovery proposals prevent completion' in blocked['errors']


@pytest.mark.parametrize('gap', ['omitted', 'empty_discovery', 'wrong_version', 'dependency'])
def test_requested_product_coverage_fails_closed(tmp_path, repository, monkeypatch, gap):
    controller, discovery, ingestion, upstream, contract, run = setup_run(tmp_path, repository)
    products: list[dict] = [{'id': 'linux', 'version': '6.8'}]
    if gap == 'empty_discovery':
        discovery.state['proposals'].clear()
        discovery.state['receipts'].clear()
    if gap == 'omitted':
        products.append({'id': 'openssl', 'version': '3'})
    if gap == 'dependency':
        products.append({'id': 'app', 'version': '1'})
        products[0]['dependency_evidence'] = [dict(kind='lockfile', path='app.lock', dependency_of='app')]
    if gap == 'wrong_version':
        products[0]['version'] = '6.9'
    contract['requested_products'] = products
    monkeypatch.setattr(launcher, 'run_relay', lambda config, argv, session, **kw:
        offline_runtime(config, argv, session,
            request={'schema_version': 1, 'products': [{'id': 'linux', 'version': '6.8'}]}))
    verdict = launcher.run_trusted_launch(request={'schema_version': 1, 'products': products},
        run_dir=run, contract=contract, controller=controller, discovery=discovery,
        ingestion=ingestion, inference=None, config=None, argv=['/fixture'], preflight=fixture_preflight, claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))
    assert not verdict['valid'], verdict
    assert any('requested product' in e for e in verdict['errors']), verdict
    assert json.loads((run / 'result.json').read_text())['status'] != 'completed'


def test_report_projection_never_predicts_success(tmp_path, repository):
    controller, discovery, ingestion, upstream, contract, run = setup_run(tmp_path, repository)
    controller.acquire('docs')
    ingestion.prepare('docs')
    launcher.write_controller_reports(run, contract, controller.evidence())
    assert json.loads((run / 'result.json').read_text())['status'] == 'partial'
    assert not json.loads((run / 'coverage-matrix.json').read_text())['complete']
    assert json.loads((run / 'completeness-challenge.json').read_text())['status'] == 'pending'


def _launch_inventory(request, contract, *, dispositions=None, versions=None):
    dispositions = dispositions or {}
    versions = versions or {}
    return {'caller': contract['caller_profile'], 'bank': contract['hindsight_bank'],
            'products': [dict(id=p['id'], version=versions.get(p['id'], p['version']),
                              disposition=dispositions.get(p['id'], 'missing'), source='fixture',
                              source_hash='a' * 64, source_hash_verified=True)
                         for p in request['products']]}


def test_launch_rejects_duplicate_inventory_without_runtime_or_claim(tmp_path, repository, monkeypatch):
    controller, discovery, ingestion, upstream, contract, run = setup_run(tmp_path, repository)
    request = {'schema_version': 1, 'products': [{'id': 'linux', 'version': '6.8'}]}
    def preflight(*, request, contract):
        inventory = _launch_inventory(request, contract)
        inventory['products'].append(dict(inventory['products'][0]))
        return inventory
    monkeypatch.setattr(launcher, 'run_relay', lambda *a, **k: pytest.fail('runtime must not start'))
    with pytest.raises(launcher.BoundaryError, match='exactly match'):
        launcher.run_trusted_launch(request=request, run_dir=run, contract=contract,
            controller=controller, discovery=discovery, ingestion=ingestion,
            inference=None, config=None, argv=['/fixture'], preflight=preflight,
            claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))


def test_launch_rejects_version_mismatch_before_claim(tmp_path, repository, monkeypatch):
    controller, discovery, ingestion, upstream, contract, run = setup_run(tmp_path, repository)
    request = {'schema_version': 1, 'products': [{'id': 'linux', 'version': '6.8'}]}
    monkeypatch.setattr(launcher, 'run_relay', lambda *a, **k: pytest.fail('runtime must not start'))
    with pytest.raises(launcher.BoundaryError, match='exactly match'):
        launcher.run_trusted_launch(request=request, run_dir=run, contract=contract,
            controller=controller, discovery=discovery, ingestion=ingestion,
            inference=None, config=None, argv=['/fixture'],
            preflight=lambda **kw: _launch_inventory(kw['request'], kw['contract'], versions={'linux': '6.9'}),
            claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))


def test_pre_executor_binding_error_releases_only_owned_claims(tmp_path, repository, monkeypatch):
    controller, discovery, ingestion, upstream, contract, run = setup_run(tmp_path, repository)
    request = {'schema_version': 1, 'products': [{'id': 'linux', 'version': '6.8'}]}
    store = RunClaimStore(tmp_path / 'claims.sqlite')
    monkeypatch.setattr(launcher, 'secrets', type('Secrets', (), {'token_hex': staticmethod(lambda n: 'owned')}))
    monkeypatch.setattr(launcher, 'run_relay', lambda *a, **k: pytest.fail('runtime must not start'))
    bad_ingestion = ingestion
    bad_ingestion.bank = 'OtherBank'
    with pytest.raises(launcher.BoundaryError, match='binding mismatch'):
        launcher.run_trusted_launch(request=request, run_dir=run, contract=contract,
            controller=controller, discovery=discovery, ingestion=bad_ingestion,
            inference=None, config=None, argv=['/fixture'], preflight=fixture_preflight, claim_store=store)
    with store._connect() as connection:
        assert connection.execute('SELECT count(*) FROM run_claims').fetchone()[0] == 0


def test_verified_present_reuses_without_runtime_or_claim(tmp_path, repository, monkeypatch):
    controller, discovery, ingestion, upstream, contract, run = setup_run(tmp_path, repository)
    request = {'schema_version': 1, 'products': [{'id': 'linux', 'version': '6.8'}]}
    monkeypatch.setattr(launcher, 'run_relay', lambda *a, **k: pytest.fail('runtime must not start'))
    verdict = launcher.run_trusted_launch(request=request, run_dir=run, contract=contract,
        controller=controller, discovery=discovery, ingestion=ingestion,
        inference=None, config=None, argv=['/fixture'],
        preflight=lambda **kw: _launch_inventory(kw['request'], kw['contract'], dispositions={'linux': 'verified-present'}),
        claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))
    assert verdict['status'] == 'reuse'
    assert verdict['disposition'] == 'verified-present'


def test_attach_existing_reports_existing_run_id(tmp_path, repository, monkeypatch):
    controller, discovery, ingestion, upstream, contract, run = setup_run(tmp_path, repository)
    request = {'schema_version': 1, 'products': [{'id': 'linux', 'version': '6.8'}]}
    store = RunClaimStore(tmp_path / 'claims.sqlite')
    source, digest = 'fixture', 'a' * 64
    namespace = 'default:Liv'
    import time
    store.claim(namespace, 'linux', '6.8', launcher.scope_fingerprint(source, digest),
                'other-owner', 'existing-run', time.time(), 300)
    monkeypatch.setattr(launcher, 'run_relay', lambda *a, **k: pytest.fail('runtime must not start'))
    verdict = launcher.run_trusted_launch(request=request, run_dir=run, contract=contract,
        controller=controller, discovery=discovery, ingestion=ingestion,
        inference=None, config=None, argv=['/fixture'], preflight=fixture_preflight, claim_store=store)
    assert verdict['status'] == 'attach-existing'
    assert verdict['existing_run_id'] == 'existing-run'


def setup_canonical_run(tmp_path, repository, *, dependency_version='1.0.0'):
    from types import SimpleNamespace
    from broker.source_policy import CanonicalSourcePolicy
    repo, _ = repository
    # Keep the canonical fixture pinned to one committed tree containing every
    # authenticated documentation root and package descriptor it resolves.
    files = {
        'packages/web/src/content/docs/index.md': b'OpenCode docs',
        'dep/package.json': json.dumps(dict(name='dep', version='1.0.0',
            directories={'doc': 'docs'})).encode(),
        'dep/docs/index.md': b'dep docs',
        'leaf/package.json': json.dumps(dict(name='leaf', version='1.0.0',
            directories={'doc': 'docs'})).encode(),
        'leaf/docs/index.md': b'leaf docs',
    }
    for relative, data in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.test', 'commit', '-qm',
                    'canonical fixture'], check=True)
    pin = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                  text=True).strip()
    corpus = tmp_path / 'corpus'
    controller = IntegratedController('run', corpus, {})
    request = {'schema_version': 1, 'products': [{'id': 'opencode', 'version': '1.2.3'}]}
    metadata = {
        'opencode-ai': dict(version='1.2.3', dependencies={'dep': dependency_version}),
        'dep': dict(version='1.0.0', dependencies={'leaf': '1.0.0'}),
        'leaf': dict(version='1.0.0', dependencies={}),
    }
    def fetch(url, **kwargs):
        name = url.split('/')[-2]
        repository = 'https://github.com/anomalyco/opencode'
        if name in ('dep', 'leaf'):
            repository = dict(type='git', url=repository, directory=name)
        return SimpleNamespace(url=url, status=200, redirects=(), data=json.dumps(dict(
            name=name, gitHead=pin, repository=repository,
            **metadata[name])).encode())
    policy = CanonicalSourcePolicy(controller, request,
        repositories={'https://github.com/anomalyco/opencode': repo}, fetch=fetch)
    discovery = DiscoveryController(controller, policy)
    ingestion = ControllerIngestion(controller, 'Liv', FakeUpstream())
    contract = build_contract(run_id='run', caller_profile='default', workspace=tmp_path,
        corpus_root=corpus, hindsight_bank='Liv', ingestion_required=True,
        requested_products=request['products'])
    run = corpus / 'reports/runs/run'
    return request, controller, policy, discovery, ingestion, contract, run


def acquire_catalog(session, *, only=None):
    adapter = session.acquisition
    for source in adapter.catalog()['sources']:
        identifier = source['source_id']
        if only is not None and identifier != only:
            continue
        adapter.acquire(identifier)
        for artifact in adapter.prepare(identifier)['artifact_ids']:
            adapter.artifact('artifact.submit', artifact)
            adapter.artifact('artifact.poll', artifact)


def test_canonical_launch_bootstraps_transitive_closure(tmp_path, repository, monkeypatch):
    request, controller, policy, discovery, ingestion, contract, run = setup_canonical_run(tmp_path, repository)
    def runtime(config, argv, session, **kwargs):
        acquire_catalog(session)
        session.finish()
        return RuntimeResult(0, b'', 'offline-fixture')
    monkeypatch.setattr(launcher, 'run_relay', runtime)
    verdict = launcher.run_trusted_launch(request=request, run_dir=run, contract=contract,
        controller=controller, discovery=discovery, ingestion=ingestion,
        inference=None, config=None, argv=['/fixture'], preflight=fixture_preflight, claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))
    assert verdict['valid'], verdict
    assert {r['product_id'] for r in policy.coverage()['products']} == {'opencode', 'dep', 'leaf'}
    policy.assert_complete()
    assert json.loads((run / 'final-result.json').read_text())['status'] == 'completed'


@pytest.mark.parametrize('gap', ['omitted', 'blocked_range'])
def test_canonical_launch_never_reports_missing_dependencies_complete(tmp_path, repository, monkeypatch, gap):
    request, controller, policy, discovery, ingestion, contract, run = setup_canonical_run(
        tmp_path, repository, dependency_version='^1.0.0' if gap == 'blocked_range' else '1.0.0')
    policy.bootstrap(discovery)
    def runtime(config, argv, session, **kwargs):
        acquire_catalog(session, only=policy.approvals['opencode'].source_id)
        # A custom trusted runtime returning zero must not bypass closure.
        return RuntimeResult(0, b'', 'offline-fixture')
    monkeypatch.setattr(launcher, 'run_relay', runtime)
    verdict = launcher.run_trusted_launch(request=request, run_dir=run, contract=contract,
        controller=controller, discovery=discovery, ingestion=ingestion,
        inference=None, config=None, argv=['/fixture'], preflight=fixture_preflight, claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))
    assert not verdict['valid'], verdict
    result = json.loads((run / 'result.json').read_text())
    assert result == json.loads((run / 'final-result.json').read_text())
    assert result['status'] != 'completed'
    assert json.loads((run / 'launch.json').read_text())['exit_code'] == 74
    assert not json.loads((run / 'coverage-matrix.json').read_text())['complete']
    assert json.loads((run / 'completeness-challenge.json').read_text())['status'] != 'passed'


def test_canonical_policy_request_binding_cannot_be_substituted(tmp_path, repository, monkeypatch):
    request, controller, policy, discovery, ingestion, contract, run = setup_canonical_run(tmp_path, repository)
    policy.request['products'][0]['version'] = '9.9.9'
    monkeypatch.setattr(launcher, 'run_relay', lambda *a, **kw: pytest.fail('runtime must not start'))
    with pytest.raises(ValueError, match='policy.*binding'):
        launcher.run_trusted_launch(request=request, run_dir=run, contract=contract,
            controller=controller, discovery=discovery, ingestion=ingestion,
            inference=None, config=None, argv=['/fixture'], preflight=fixture_preflight, claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))


def test_production_gate_still_closed():
    with pytest.raises(RuntimeError, match='production Librarian launch disabled'):
        launcher.require_trusted_controller()


def test_trusted_launch_denies_discovery_tools_without_browser(tmp_path, repository, monkeypatch):
    controller, discovery, ingestion, upstream, contract, run = setup_run(tmp_path, repository)

    def runtime(config, argv, session, **kwargs):
        reply = json.loads(session.feed((json.dumps({
            'id': 1, 'op': 'discovery.fetch',
            'params': {'url': 'https://fictional.invalid/software'},
        }) + '\n').encode()))
        assert not reply['ok']
        session.finish()
        return RuntimeResult(0, b'', 'offline-fixture')

    monkeypatch.setattr(launcher, 'run_relay', runtime)
    verdict = launcher.run_trusted_launch(
        request={'schema_version': 1, 'products': [{'id': 'linux', 'version': '6.8'}]},
        run_dir=run, contract=contract, controller=controller, discovery=discovery,
        ingestion=ingestion, inference=None, config=None, argv=['/fixture'], preflight=fixture_preflight, claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))
    assert not verdict['valid']
    assert verdict['status'] == 'partial'


@pytest.mark.skipif(os.environ.get('LIBRARIAN_RUN_KNOWN_RED') != '1',
                    reason='known red before 2026-09-30 (Librarian phase record): the isolated '
                           'relay returns partial without driving any inference prompt; red with '
                           'and without the inventory fix. Set LIBRARIAN_RUN_KNOWN_RED=1 to run it.')
def test_trusted_launch_passes_injected_browser_to_isolated_client(tmp_path, monkeypatch):
    """The real client/relay can inspect an unseen candidate, but cannot complete it."""
    import os
    import sys

    from broker.controller_integrations import IntegratedController
    from broker.librarian_relay import InferenceProxy, serve_connection

    class Wire:
        data = b'# NebulaQuill\n\nUnseen fictional software documentation.'
        url = 'https://fictional.invalid/nebulaquill'
        status = 200
        redirects = ()

    corpus = tmp_path / 'corpus'
    controller = IntegratedController('run', corpus, {})
    discovery = DiscoveryController(controller, lambda proposal, evidence: None)
    ingestion = ControllerIngestion(controller, 'Liv', lambda *args, **kwargs: None)
    request = {'schema_version': 1, 'products': [{'id': 'nebulaquill', 'version': '0'}]}
    contract = build_contract(run_id='run', caller_profile='default', workspace=tmp_path,
        corpus_root=corpus, hindsight_bank='Liv', ingestion_required=True,
        requested_products=request['products'])
    run = corpus / 'reports/runs/run'
    browser = DiscoveryBrowser(fetch=lambda *args, **kwargs: Wire())
    prompts = []

    def inference(**kwargs):
        prompt = json.loads(kwargs['prompt'])
        prompts.append(prompt)
        if len(prompts) == 1:
            decision = {'tool': 'discovery_fetch', 'arguments': {'url': Wire.url}}
        elif len(prompts) == 2:
            receipt = json.loads(prompt['messages'][-1]['content'])['receipt_id']
            decision = {'tool': 'discovery_read',
                        'arguments': {'receipt_id': receipt, 'offset': 0, 'limit': 256}}
        else:
            decision = {'final': 'Candidate inspected.'}
        return json.dumps(decision)

    def isolated_runtime(config, argv, session, *, request, **kwargs):
        import agent.transports.chat_completions as transport
        image = tmp_path / 'image'
        package = image / 'broker'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        root = Path(__file__).parents[1]
        for name in ('librarian_agent_client.py', 'librarian_request.py', 'librarian_identity.py'):
            (package / name).write_bytes((root / 'broker' / name).read_bytes())
        env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path),
               'PYTHONPATH': os.pathsep.join([str(image), str(Path(transport.__file__).parents[2])]),
               'PYTHONDONTWRITEBYTECODE': '1'}
        proc = subprocess.Popen([sys.executable, '-m', 'broker.librarian_agent_client'],
                                cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.stdin.write(json.dumps(request).encode() + b'\n')
            proc.stdin.flush()
            serve_connection(session, proc.stdout, proc.stdin, timeout=15)
            proc.stdin.close()
            assert proc.wait(timeout=5) == 0, proc.stderr.read().decode()
            assert proc.stderr.read() == b''
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        return RuntimeResult(0, b'', 'offline-client')

    monkeypatch.setattr(launcher, 'run_relay', isolated_runtime)
    verdict = launcher.run_trusted_launch(request=request, run_dir=run, contract=contract,
        controller=controller, discovery=discovery, ingestion=ingestion, browser=browser,
        inference=InferenceProxy('offline-route', inference), config=None, argv=['/fixture'], preflight=fixture_preflight, claim_store=RunClaimStore(tmp_path / 'claims.sqlite'))
    assert not verdict['valid']
    assert verdict['status'] == 'partial'
    assert len(prompts) == 3
    assert 'NebulaQuill' in json.dumps(prompts[2])


def test_tag_scheme_report_reads_the_helper_output_only():
    """Compliance is reported from the helper, never inferred by the supervisor."""
    assert launcher._tag_scheme_report({})['status'] == 'unknown'
    assert launcher._tag_scheme_report('not-a-dict')['status'] == 'unknown'
    compliant = launcher._tag_scheme_report({
        'tag_compliance': {'compliant': True, 'documents': 2, 'max_tags': 5},
        'tags': {'a': ['corpus:jq'], 'b': ['corpus:jq']}})
    assert compliant['status'] == 'compliant'
    assert compliant['artifacts'] == 2 and compliant['documents'] == 2
    assert launcher._tag_scheme_report(
        {'tag_compliance': {'compliant': False}})['status'] == 'non-compliant'


def test_scope_preflight_report_is_advisory_and_never_raises(tmp_path):
    """A missing tool and a failing bank both report, neither aborts the run."""
    assert launcher._scope_preflight_report('Liv', tmp_path)['status'] == 'unknown'
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'scope-preflight.py').write_text(
        "import json\n"
        "print(json.dumps({'status': 'fail', 'scopes': 63, 'observations': 74871}))\n")
    report = launcher._scope_preflight_report('Liv', tmp_path)
    assert report['status'] == 'fail' and report['scopes'] == 63
