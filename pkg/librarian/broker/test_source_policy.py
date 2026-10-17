import importlib
import pytest


@pytest.mark.parametrize('change,reason', [
    ({'gitHead': 'abc123'}, 'invalid-commit-pin'),
    ({'repository': 'https://github.com/attacker/opencode'}, 'metadata-repository-mismatch'),
    ({'version': '2.0.0'}, 'metadata-identity-mismatch'),
])
def test_untrusted_metadata_cannot_authorize(tmp_path, change, reason):
    from broker.source_policy import CanonicalSourcePolicy
    repo, pin = repository(tmp_path)
    metadata = {'name': 'opencode-ai', 'version': '1.2.3', 'gitHead': pin,
                'repository': 'https://github.com/anomalyco/opencode'}
    metadata.update(change)
    controller = IntegratedController('run', tmp_path / 'corpus', {})
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': 'opencode', 'version': '1.2.3'}]}, repositories={
        'https://github.com/anomalyco/opencode': repo}, fetch=lambda url, **kw:
        SimpleNamespace(url=url, status=200, redirects=(), data=json.dumps(metadata).encode()))
    discovery = DiscoveryController(controller, policy)
    assert policy.bootstrap(discovery)[0]['status'] == 'unresolved'
    assert policy.coverage()['products'][0]['reason'] == reason


def test_bootstrap_handles_unrepresentable_dependency_ranges(tmp_path):
    from broker.source_policy import CanonicalSourcePolicy
    repo, pin = repository(tmp_path)
    controller = IntegratedController('run', tmp_path / 'corpus', {})
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': 'opencode', 'version': '1.2.3'}]}, repositories={
        'https://github.com/anomalyco/opencode': repo}, fetch=lambda url, **kw:
        SimpleNamespace(url=url, status=200, redirects=(), data=json.dumps({
            'name': 'opencode-ai', 'version': '1.2.3', 'gitHead': pin,
            'repository': 'https://github.com/anomalyco/opencode',
            'dependencies': {'dep': '^1.0.0'}}).encode()))
    discovery = DiscoveryController(controller, policy)
    policy.bootstrap(discovery)
    policy.bootstrap(discovery)
    assert policy.coverage()['status'] == 'blocked'


def test_requested_dependency_reference_needs_verified_local_evidence(tmp_path):
    from broker.source_policy import CanonicalSourcePolicy
    repo, pin = repository(tmp_path)
    controller = IntegratedController('run', tmp_path / 'corpus', {})
    request = {'schema_version': 1, 'products': [
        {'id': 'opencode', 'version': '1.2.3'},
        {'id': 'opencode-ai', 'version': '1.2.3', 'dependency_evidence': [
            {'kind': 'configuration', 'path': 'not-read.json', 'dependency_of': 'opencode'}]}]}
    policy = CanonicalSourcePolicy(controller, request, repositories={
        'https://github.com/anomalyco/opencode': repo}, fetch=lambda url, **kw:
        SimpleNamespace(url=url, status=200, redirects=(), data=json.dumps({
            'name': 'opencode-ai', 'version': '1.2.3', 'gitHead': pin,
            'repository': 'https://github.com/anomalyco/opencode'}).encode()))
    discovery = DiscoveryController(controller, policy)
    for result in policy.bootstrap(discovery):
        if result['status'] == 'approved':
            controller.acquire(result['source_ids'][0])
    assert policy.coverage()['status'] == 'blocked'


def test_linux_official_release_and_local_tag(tmp_path):
    from broker.source_policy import CanonicalSourcePolicy
    repo, pin = repository(tmp_path)
    subprocess.run(['git', '-C', str(repo), 'tag', 'v6.8'], check=True)
    controller = IntegratedController('linux-run', tmp_path / 'corpus', {})
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': 'linux', 'version': '6.8'}]}, repositories={
            'https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git': repo},
        fetch=lambda url, **kw: SimpleNamespace(url=url, status=200, redirects=(), data=json.dumps({
            'releases': [{'version': '6.8', 'source': 'https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.8.tar.xz'}]}).encode()))
    discovery = DiscoveryController(controller, policy)
    results = policy.bootstrap(discovery)
    assert results[0]['status'] == 'approved'
    assert controller.plan[results[0]['source_ids'][0]]['commit'] == pin


def test_configured_plugins_and_metadata_dependencies_cannot_be_omitted(tmp_path):
    from broker.source_policy import CanonicalSourcePolicy
    repo, pin = repository(tmp_path)
    controller = IntegratedController('run', tmp_path / 'corpus', {})
    def fetch(url, **kw):
        return SimpleNamespace(url=url, status=200, redirects=(), data=json.dumps({
            'name': 'opencode-ai', 'version': '1.2.3', 'gitHead': pin,
            'repository': 'https://github.com/anomalyco/opencode.git',
            'dependencies': {'runtime-dep': '^2.0.0'}}).encode())
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': 'opencode', 'version': '1.2.3'}]}, repositories={
        'https://github.com/anomalyco/opencode': repo}, fetch=fetch,
        configurations={'opencode': b'{"plugin":["my-plugin@2.3.4"],"purpose":"PRIVATE"}'})
    discovery = DiscoveryController(controller, policy)
    first = policy.bootstrap(discovery)[0]
    controller.acquire(first['source_ids'][0])
    coverage = policy.coverage()
    assert coverage['status'] == 'blocked'
    assert {r['product_id'] for r in coverage['products']} == {'opencode', 'my-plugin', 'runtime-dep'}
    assert 'PRIVATE' not in json.dumps(controller.evidence().snapshot())
    with pytest.raises(ValueError):
        policy.assert_complete()


def test_ubuntu_metadata_is_not_falsely_complete(tmp_path):
    from broker.source_policy import CanonicalSourcePolicy
    controller = IntegratedController('run', tmp_path, {})
    calls = []
    def fetch(url, **kw):
        calls.append(url)
        return SimpleNamespace(url=url, status=200, redirects=(), data=json.dumps({
            'entries': [{'source_package_name': 'bcmwl', 'source_package_version': '6.30.223.271+bdcom-0ubuntu10',
                         'distro_series_link': 'https://api.launchpad.net/1.0/ubuntu/noble'}]}).encode())
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': 'ubuntu/noble/bcmwl', 'version': '6.30.223.271+bdcom-0ubuntu10'}]}, repositories={}, fetch=fetch)
    discovery = DiscoveryController(controller, policy)
    assert policy.bootstrap(discovery)[0]['status'] == 'unresolved'
    assert policy.coverage()['products'][0]['reason'] == 'resolution-failed'
    assert calls[0].startswith('https://api.launchpad.net/1.0/ubuntu/+archive/primary?')


def test_ubuntu_adapter_resolves_archive_and_keeps_receipt_dependencies_blocked(tmp_path):
    from broker.controller_integrations import IntegratedController
    from broker.librarian_discovery import DiscoveryController
    from broker.source_policy import CanonicalSourcePolicy
    from broker.test_ubuntu_sources import fixture
    from broker.ubuntu_sources import UbuntuSourceAdapter
    product, version, _, _, fetch = fixture()
    adapter = UbuntuSourceAdapter(fetch=fetch)
    controller = IntegratedController('ubuntu-policy-run', tmp_path, {}, ubuntu_adapter=adapter)
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': product, 'version': version}]}, repositories={}, fetch=fetch)
    discovery = DiscoveryController(controller, policy)
    result = discovery.propose('source', {'product_id': product, 'version': version,
                                          'candidate_id': 'canonical'})
    assert result['status'] == 'approved'
    source_id = result['source_ids'][0]
    assert controller.acquire(source_id)['status'] == 'complete'
    assert controller.plan[source_id]['kind'] == 'ubuntu-source'
    from broker.controller_integrations import ControllerIngestion
    from broker.test_controller_integrations import FakeUpstream
    artifacts = ControllerIngestion(controller, 'Liv', FakeUpstream()).prepare(source_id)
    assert artifacts and all(artifact['occurrence_ids'] for artifact in artifacts)
    assert policy.coverage()['status'] == 'blocked'
    assert all(row['status'] == 'unresolved' for row in policy.coverage()['products']
               if row['product_id'].startswith('ubuntu-binary/'))
import json
import subprocess
from types import SimpleNamespace

from broker.controller_integrations import IntegratedController
from broker.librarian_discovery import DiscoveryController


def test_unknown_identity_is_explicitly_unresolved(tmp_path):
    module = importlib.import_module('broker.source_policy')
    controller = IntegratedController('run', tmp_path, {})
    policy = module.CanonicalSourcePolicy(controller, {
        'schema_version': 1, 'products': [{'id': 'unknown-tool', 'version': '1'}]},
        repositories={})
    assert policy({'product_id': 'unknown-tool', 'version': '1', 'candidate_id': 'anything'}, controller.evidence().snapshot()) is None
    assert policy.coverage()['status'] == 'blocked'
    assert policy.coverage()['products'][0]['reason'] == 'unsupported-product'


def repository(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'README.md').write_text('Full documentation\n')
    (repo / 'other.txt').write_text('Not a hand-selected documentation subset\n')
    for root in ('Documentation', 'packages/web/src/content/docs'):
        (repo / root).mkdir(parents=True)
        (repo / root / 'index.md').write_text('Complete documentation root\n')
        (repo / root / 'asset.bin').write_bytes(b'\x00\xff')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    pin = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()
    return repo, pin


def test_npm_metadata_binds_version_and_enumerates_whole_documentation_root(tmp_path):
    from broker.source_policy import CanonicalSourcePolicy
    repo, pin = repository(tmp_path)
    controller = IntegratedController('run', tmp_path / 'corpus', {})
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(url=url, status=200, redirects=(), data=json.dumps({
            'name': 'opencode-ai', 'version': '1.2.3', 'gitHead': pin,
            'repository': {'url': 'git+https://github.com/anomalyco/opencode.git'},
            'dependencies': {}}).encode())
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': 'opencode', 'version': '1.2.3'}]}, repositories={
            'https://github.com/anomalyco/opencode': repo}, fetch=fetch)
    discovery = DiscoveryController(controller, policy)
    result = discovery.propose('source', {'product_id': 'opencode', 'version': '1.2.3', 'candidate_id': 'ignored'})
    assert result['status'] == 'approved'
    assert policy.coverage()['status'] == 'blocked'
    acquired = controller.acquire(result['source_ids'][0])
    assert acquired['status'] == 'complete'
    assert {e['path'] for e in acquired['entries']} == {
        'packages/web/src/content/docs/index.md', 'packages/web/src/content/docs/asset.bin'}
    assert acquired['index_path'].endswith('local-index.json')
    assert policy.coverage()['status'] == 'ready'
    assert calls == ['https://registry.npmjs.org/opencode-ai/1.2.3']
    assert policy(controller.plan, controller.evidence().snapshot()) is None


def test_wrong_registry_version_never_approves(tmp_path):
    from broker.source_policy import CanonicalSourcePolicy
    controller = IntegratedController('run', tmp_path, {})
    policy = CanonicalSourcePolicy(controller, {'schema_version': 1, 'products': [
        {'id': 'opencode', 'version': '1.2.3'}]}, repositories={}, fetch=lambda url, **kw:
        SimpleNamespace(url=url, status=200, redirects=(), data=b'{"name":"opencode-ai","version":"9.0.0"}'))
    assert policy({'product_id': 'opencode', 'version': '1.2.3', 'candidate_id': 'x'}, controller.evidence().snapshot()) is None
    assert policy.coverage()['products'][0]['reason'] == 'metadata-identity-mismatch'
