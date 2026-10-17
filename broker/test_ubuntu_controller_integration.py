"""Offline archive-to-durable-ingestion integration coverage."""
import pytest

from broker.acquisition_controller import BoundaryError
from broker.controller_integrations import ControllerIngestion, IntegratedController, validate_ingestion
from broker.librarian_discovery import DiscoveryController, SourceApproval
from broker.test_controller_integrations import FakeUpstream
from broker.test_ubuntu_sources import fixture
from broker.ubuntu_sources import UbuntuSourceAdapter, fingerprint


def setup_chain(tmp_path):
    product, version, data, calls, fetch = fixture()
    adapter = UbuntuSourceAdapter(fetch=fetch)
    plan, _ = adapter.resolve(product, version)
    controller = IntegratedController('ubuntu-run', tmp_path, {}, ubuntu_adapter=adapter)
    discovery = DiscoveryController(controller, lambda proposal, evidence:
        SourceApproval('ubuntu', plan, 'fixture-policy'))
    result = discovery.propose('source', dict(product_id=product, version=version, candidate_id='canonical'))
    assert result['status'] == 'approved'
    return controller, discovery, plan, data


def test_archive_to_durable_ingestion_preserves_evidence_and_closure(tmp_path):
    controller, discovery, plan, _ = setup_chain(tmp_path)
    receipt = controller.acquire('ubuntu')
    assert receipt['status'] == 'complete', receipt
    assert receipt['archive_manifest_sha256'] == plan['archive_manifest_sha256']
    assert receipt['product_id'] == plan['product_id']
    assert receipt['version'] == plan['version']
    assert len(receipt['attempts']) == len(plan['artifacts'])
    upstream = FakeUpstream()
    ingestion = ControllerIngestion(controller, 'Liv', upstream, caller='fixture')
    artifacts = ingestion.prepare('ubuntu')
    assert {i for a in artifacts for i in a['occurrence_ids']} == {
        o['id'] for o in receipt['occurrences'] if o['path'].endswith(('README', 'debian/control'))}
    classifications = ingestion.state['classifications']
    assets = [o for o in receipt['occurrences'] if o['path'].endswith('.tar.gz')]
    assert assets
    assert all(classifications[o['id']]['classification'] == 'asset-only' for o in assets)
    assert all(classifications[o['id']]['sha256'] == o['sha256'] for o in assets)
    for artifact in artifacts:
        assert ingestion.submit(artifact['id'])['status'] == 'accepted'
        assert ingestion.poll(artifact['id'])['status'] == 'completed'
    posts = len([r for r in upstream.requests if r.method == 'POST'])
    restarted = IntegratedController('ubuntu-run', tmp_path, controller.plan)
    resumed = ControllerIngestion(restarted, 'Liv', upstream, caller='fixture')
    assert restarted.evidence().snapshot()['sources']['ubuntu'] == receipt
    for artifact in resumed.prepare('ubuntu'):
        assert resumed.submit(artifact['id'])['status'] == 'completed'
    assert len([r for r in upstream.requests if r.method == 'POST']) == posts
    contract = dict(run_id='ubuntu-run', corpus_root=str(tmp_path), hindsight_bank='Liv')
    assert any('dependenc' in error for error in validate_ingestion(restarted.evidence(), contract, artifacts))
    assert discovery.completion()['status'] == 'blocked'
    assert discovery.completion()['unresolved_dependencies'] == receipt['dependencies']


@pytest.mark.parametrize('artifact_index', [0, 1])
def test_corrupt_artifact_never_prepares_or_posts(tmp_path, artifact_index):
    controller, _, plan, data = setup_chain(tmp_path)
    url = plan['artifacts'][artifact_index]['url']
    data[url] = b'X' * len(data[url])
    receipt = controller.acquire('ubuntu')
    assert receipt['status'] == 'blocked'
    assert 'SHA256' in receipt['error']
    assert receipt['occurrences'] == []
    upstream = FakeUpstream()
    ingestion = ControllerIngestion(controller, 'Liv', upstream)
    with pytest.raises(BoundaryError, match='complete'):
        ingestion.prepare('ubuntu')
    assert upstream.requests == []


@pytest.mark.parametrize('change', ['unknown-field', 'missing-hash', 'duplicate', 'foreign-url', 'manifest'])
def test_discovery_rejects_invalid_trusted_archive_plan(tmp_path, change):
    controller, discovery, original, _ = setup_chain(tmp_path)
    import copy
    plan = copy.deepcopy(original)
    if change == 'unknown-field':
        plan['adapter'] = 'agent.module:execute'
    elif change == 'missing-hash':
        del plan['artifacts'][0]['sha256']
    elif change == 'duplicate':
        plan['artifacts'].append(plan['artifacts'][0])
    elif change == 'foreign-url':
        plan['artifacts'][0]['url'] = 'https://attacker.example/bcmwl.dsc'
    if change != 'manifest':
        plan['archive_manifest_sha256'] = fingerprint(plan['artifacts'])
    else:
        plan['archive_manifest_sha256'] = '0' * 64
    assert not discovery._valid_plan(plan)
    controller.plan['invalid'] = plan
    assert controller.acquire('invalid')['status'] == 'blocked'


def test_discovery_preserves_exact_debian_epoch_and_tilde(tmp_path):
    from broker.librarian_discovery import validate_proposal
    proposal = dict(product_id='ubuntu/noble/bcmwl', version='1:6.30~rc1-0ubuntu1', candidate_id='canonical')
    validate_proposal('source', proposal)
    assert proposal['version'] == '1:6.30~rc1-0ubuntu1'


def test_malformed_verified_tar_is_blocked_not_uncaught(tmp_path):
    import gzip
    product, version, _, _, fetch = fixture(gzip.compress(b'not a tar archive'))
    adapter = UbuntuSourceAdapter(fetch=fetch)
    plan, _ = adapter.resolve(product, version)
    controller = IntegratedController('bad-tar', tmp_path, {'ubuntu': plan}, ubuntu_adapter=adapter)
    assert controller.acquire('ubuntu')['status'] == 'blocked'


def test_missing_dependency_inventory_blocks_discovery(tmp_path):
    controller, discovery, _, _ = setup_chain(tmp_path)
    assert controller.acquire('ubuntu')['status'] == 'complete'
    del controller._state['sources']['ubuntu']['dependencies']
    assert discovery.completion()['status'] == 'blocked'
