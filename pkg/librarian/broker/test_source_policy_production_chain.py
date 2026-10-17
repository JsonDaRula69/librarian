"""Offline fixtures exercising the actual launcher controller chain."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from broker.acquisition_controller import BoundaryError
from broker.controller_integrations import ControllerIngestion, IntegratedController, validate_ingestion
from broker.librarian_discovery import DiscoveryController
from broker.proxy import UpstreamResponse
from broker.source_policy import CanonicalSourcePolicy


KERNEL = 'https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git'
OPENCODE = 'https://github.com/anomalyco/opencode'


def repository(tmp_path, files, tag=None):
    repo = tmp_path / 'repo'
    repo.mkdir(parents=True)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args]).decode().strip()
    git('init', '-q')
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
    git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
    if tag:
        git('tag', tag)
    return repo, git('rev-parse', 'HEAD')


def test_linux_real_production_chain_preserves_every_documentation_blob(tmp_path):
    files = {'Documentation/index.rst': 'Kernel reference',
             'Documentation/admin-guide/network.rst': 'Networking reference',
             'Documentation/schema.yaml': 'type: object',
             'Documentation/logo.bin': b'\x00\xff',
             'kernel/main.c': 'not documentation', 'README': 'not the doc boundary'}
    repo, pin = repository(tmp_path, files, 'v6.8')
    controller = IntegratedController('linux-chain', tmp_path / 'corpus', {})
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': 'linux', 'version': '6.8'}]}, repositories={KERNEL: repo},
        fetch=lambda url, **kw: SimpleNamespace(url=url, status=200, redirects=(),
                                               data=b'{"releases":[{"version":"6.8"}]}'))
    discovery = DiscoveryController(controller, policy)
    result = policy.bootstrap(discovery)[0]
    assert result['status'] == 'approved'
    source = result['source_ids'][0]
    receipt = controller.acquire(source)
    assert receipt['status'] == 'complete', receipt
    assert controller.plan[source]['documentation_roots'] == ['Documentation']
    assert receipt['commit'] == pin
    expected = {name for name in files if name.startswith('Documentation/')}
    assert {row['path'] for row in receipt['occurrences']} == expected
    exercise_ingestion(controller, policy, discovery, [source], expected)


def test_opencode_and_documented_plugin_real_production_chain(tmp_path):
    web = 'packages/web/src/content/docs'
    app_files = {web + '/index.md': 'Website docs', web + '/plugins.md': 'Plugin docs',
                 web + '/asset.bin': b'\x00\xff', 'packages/opencode/src/main.ts': 'code'}
    app_repo, app_pin = repository(tmp_path / 'app', app_files)
    plugin_files = {'package.json': json.dumps({'name': 'example-plugin', 'version': '2.3.4',
                                              'directories': {'doc': 'manual'}}),
                    'README.md': 'Plugin overview', 'manual/setup.md': 'Plugin setup',
                    'manual/data.json': '{"schema":1}', 'src/main.ts': 'plugin code'}
    plugin_repo, plugin_pin = repository(tmp_path / 'plugin', plugin_files)
    plugin_origin = 'https://github.com/example/plugin'
    metadata = {'https://registry.npmjs.org/opencode-ai/1.2.3': {
        'name': 'opencode-ai', 'version': '1.2.3', 'gitHead': app_pin, 'repository': OPENCODE},
        'https://registry.npmjs.org/example-plugin/2.3.4': {
        'name': 'example-plugin', 'version': '2.3.4', 'gitHead': plugin_pin, 'repository': plugin_origin}}
    def fetch(url, **kw):
        return SimpleNamespace(url=url, status=200, redirects=(), data=json.dumps(metadata[url]).encode())
    controller = IntegratedController('opencode-chain', tmp_path / 'corpus', {})
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': 'opencode', 'version': '1.2.3'}]}, repositories={OPENCODE: app_repo, plugin_origin: plugin_repo},
        fetch=fetch, configurations={'opencode': '{"plugin":["example-plugin@2.3.4"]}'})
    discovery = DiscoveryController(controller, policy)
    parent = discovery.propose('source', {'product_id': 'opencode', 'version': '1.2.3', 'candidate_id': 'ignored'})
    assert parent['status'] == 'approved'
    parent_id = parent['source_ids'][0]
    assert controller.acquire(parent_id)['status'] == 'complete'
    with pytest.raises(BoundaryError):
        policy.assert_complete()
    child = discovery.propose('component', {'product_id': 'example-plugin', 'version': '2.3.4',
        'dependency_of': 'opencode', 'evidence_source_ids': [parent_id]})
    assert child['status'] == 'approved'
    child_id = child['source_ids'][0]
    assert controller.acquire(child_id)['status'] == 'complete'
    assert controller.plan[parent_id]['documentation_roots'] == [web]
    assert controller.plan[child_id]['documentation_roots'] == ['README.md', 'manual']
    expected = set(app_files) - {'packages/opencode/src/main.ts'}
    expected.update({'README.md', 'manual/setup.md', 'manual/data.json'})
    exercise_ingestion(controller, policy, discovery, [parent_id, child_id], expected)


@pytest.mark.parametrize('doc,reason', [(None, 'documentation-boundary-unresolved'),
    ('.', 'documentation-boundary-unresolved'), ('../manual', 'documentation-boundary-unresolved'),
    ('missing', 'documentation-root-not-present'), ('manual/page.md', 'documentation-root-must-be-directory')])
def test_unknown_or_page_only_plugin_boundaries_block(tmp_path, doc, reason):
    files = {'package.json': json.dumps({'name': 'example-plugin', 'version': '2.3.4',
                                        'directories': {'doc': doc}}),
             'README.md': 'Do not infer closure', 'manual/page.md': 'one page'}
    repo, pin = repository(tmp_path, files)
    origin = 'https://github.com/example/plugin'
    controller = IntegratedController('blocked', tmp_path / 'corpus', {})
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': 'opencode', 'version': '1.2.3'}]}, repositories={origin: repo},
        configurations={'opencode': '{"plugin":["example-plugin@2.3.4"]}'},
        fetch=lambda url, **kw: SimpleNamespace(url=url, status=200, redirects=(), data=json.dumps({
            'name': 'example-plugin', 'version': '2.3.4', 'gitHead': pin, 'repository': origin}).encode()))
    discovery = DiscoveryController(controller, policy)
    assert discovery.propose('source', {'product_id': 'example-plugin', 'version': '2.3.4',
                                      'candidate_id': 'manual'})['status'] == 'unresolved'
    assert next(r for r in policy.coverage()['products'] if r['product_id'] == 'example-plugin')['reason'] == reason
    assert not controller.plan
    with pytest.raises(BoundaryError):
        policy.assert_complete()


@pytest.mark.parametrize('product,version,origin,tag', [
    ('linux', '6.8', KERNEL, 'v6.8'), ('opencode', '1.2.3', OPENCODE, None)])
def test_uncommitted_documentation_roots_cannot_authorize(tmp_path, product, version, origin, tag):
    repo, pin = repository(tmp_path, {'README.md': 'Only code is pinned'}, tag)
    for root in ('Documentation', 'packages/web/src/content/docs'):
        (repo / root).mkdir(parents=True)
        (repo / root / 'index.md').write_text('Untrusted checkout change')
    controller = IntegratedController('missing-roots', tmp_path / 'corpus', {})
    data = {'releases': [{'version': version}]} if product == 'linux' else {
        'name': 'opencode-ai', 'version': version, 'gitHead': pin, 'repository': origin}
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': product, 'version': version}]}, repositories={origin: repo},
        fetch=lambda url, **kw: SimpleNamespace(url=url, status=200, redirects=(), data=json.dumps(data).encode()))
    discovery = DiscoveryController(controller, policy)
    assert policy.bootstrap(discovery)[0]['status'] == 'unresolved'
    assert policy.coverage()['products'][0]['reason'] == 'documentation-root-not-present'
    assert not controller.plan


def exercise_ingestion(controller, policy, discovery, sources, expected):
    calls = []
    def transport(request):
        calls.append(request)
        if request.method == 'POST':
            return UpstreamResponse(202, json.dumps({'operation_ids': ['op_' + str(len(calls))]}).encode())
        return UpstreamResponse(200, b'{"status":"completed"}')
    ingestion = ControllerIngestion(controller, 'Liv', transport)
    artifacts = [artifact for source in sources for artifact in ingestion.prepare(source)]
    contract = {'run_id': controller._state['run_id'], 'corpus_root': str(controller.root),
                'hindsight_bank': 'Liv'}
    assert validate_ingestion(controller.evidence(), contract, artifacts)
    for artifact in artifacts:
        assert ingestion.submit(artifact['id'])['status'] == 'accepted'
        assert ingestion.poll(artifact['id'])['status'] == 'completed'
    assert validate_ingestion(controller.evidence(), contract, artifacts) == []
    assets = {p for p in expected if p.endswith('.bin')}
    assert {p['path'] for a in artifacts for p in a['provenance']} == expected - assets
    classifications = controller.evidence().snapshot()['ingestion']['classifications']
    occurrences = controller.evidence().snapshot()['sources']
    assert all(classifications[o['id']]['classification'] == 'asset-only'
               for source in sources for o in occurrences[source]['occurrences']
               if o['path'] in assets)
    assert len(calls) == len(artifacts) * 2
    assert not any(a['representation'] == 'base64' for a in artifacts)
    discovery.assert_complete()
    policy.assert_complete()
    assert policy.coverage()['status'] == 'ready'
