"""Offline trusted discovery boundary tests."""
import pytest

from broker.acquisition_controller import AcquisitionController
from broker.test_controller_integrations import repository


def test_catalog_is_projection_and_approval_expands_trusted_plan(tmp_path):
    from broker.librarian_discovery import DiscoveryController, SourceApproval
    controller = AcquisitionController('run', tmp_path, {})
    plan = {'kind': 'index', 'url': 'https://approved.example/docs/index.md',
            'boundary': 'https://approved.example/docs'}
    calls = []
    def policy(proposal, evidence):
        calls.append(proposal)
        return SourceApproval('docs', plan, 'operator-policy-v1')
    discovery = DiscoveryController(controller, policy)
    assert discovery.catalog() == {'sources': [], 'components': [], 'proposals': []}
    result = discovery.propose('source', {'product_id': 'widget', 'version': 'unknown',
                                          'candidate_id': 'vendor-docs'})
    assert result['status'] == 'approved'
    assert result['source_ids'] == ['docs']
    assert controller.plan == controller.evidence().snapshot()['plan'] == {'docs': plan}
    assert calls[0]['candidate_id'] == 'vendor-docs'
    assert discovery.catalog()['sources'] == [{'source_id': 'docs'}]
    receipt = controller.evidence().snapshot()['discovery']['receipts'][result['receipt_id']]
    assert receipt['policy_id'] == 'operator-policy-v1'
    assert receipt['run_id'] == 'run'
    assert 'url' not in str(discovery.catalog())


def test_relay_lists_expanded_sources_and_blocks_unresolved(tmp_path):
    import json
    from broker.librarian_discovery import DiscoveryController, SourceApproval
    from broker.librarian_relay import ControllerAdapter, RelaySession
    controller = AcquisitionController('run', tmp_path, {})
    discovery = DiscoveryController(controller, lambda p, e: None)
    adapter = ControllerAdapter(controller, discovery=discovery)
    session = RelaySession(adapter)
    def call(op, params):
        return json.loads(session.feed(json.dumps({'id': 1, 'op': op, 'params': params}).encode() + b'\n'))
    assert call('source.list', {})['result']['sources'] == []
    proposal = {'product_id': 'widget', 'version': 'unknown', 'candidate_id': 'vendor'}
    assert call('source.propose', proposal)['result']['status'] == 'unresolved'
    assert call('discovery.status', {})['result']['status'] == 'blocked'
    with pytest.raises(ValueError, match='unresolved'):
        discovery.assert_complete()
    discovery.policy = lambda p, e: SourceApproval('new', {
        'kind': 'index', 'url': 'https://approved.example/docs/index.md',
        'boundary': 'https://approved.example/docs'}, 'policy')
    assert call('source.propose', proposal)['result']['status'] == 'approved'
    assert 'new' in adapter.sources
    assert call('source.list', {})['result']['sources'] == [{'source_id': 'new'}]
    discovery.assert_complete()


@pytest.mark.parametrize('extra', ['official', 'url', 'path', 'purpose', 'task', 'receipt_id'])
def test_model_authority_and_access_fields_rejected(tmp_path, extra):
    from broker.librarian_discovery import DiscoveryController
    discovery = DiscoveryController(AcquisitionController('run', tmp_path, {}),
                                    lambda p, e: pytest.fail('policy must not run'))
    with pytest.raises(ValueError):
        discovery.propose('source', {'product_id': 'widget', 'version': 'unknown',
                                    'candidate_id': 'vendor', extra: 'official'})


def test_component_requires_acquired_evidence_and_independent_policy(tmp_path):
    from broker.librarian_discovery import DiscoveryController
    controller = AcquisitionController('run', tmp_path, {'docs': {'kind': 'index'}})
    calls = []
    discovery = DiscoveryController(controller, lambda p, e: calls.append(e))
    proposal = {'product_id': 'dependency', 'version': 'unknown', 'dependency_of': 'widget',
                'evidence_source_ids': ['docs']}
    assert discovery.propose('component', proposal)['status'] == 'unresolved'
    assert calls == []
    controller._state['sources']['docs'] = {'status': 'complete'}
    assert discovery.propose('component', proposal)['status'] == 'unresolved'
    assert len(calls) == 1


def test_unresolved_discovery_rejects_relay_eof(tmp_path):
    from broker.librarian_discovery import DiscoveryController
    from broker.librarian_relay import ControllerAdapter, RelaySession
    controller = AcquisitionController('run', tmp_path, {})
    discovery = DiscoveryController(controller, lambda p, e: None)
    discovery.propose('source', {'product_id': 'widget', 'version': 'unknown', 'candidate_id': 'vendor'})
    session = RelaySession(ControllerAdapter(controller, discovery=discovery))
    with pytest.raises(ValueError):
        session.finish()
    assert session.closed


@pytest.mark.parametrize('plan', [
    {'kind': 'index'},
    {'kind': 'git', 'repository': '/tmp/repo', 'commit': 'main'},
    {'kind': 'index', 'url': 'file:///etc/passwd', 'boundary': 'file:///etc'},
])
def test_invalid_policy_plan_stays_unresolved(tmp_path, plan):
    from broker.librarian_discovery import DiscoveryController, SourceApproval
    controller = AcquisitionController('run', tmp_path, {})
    discovery = DiscoveryController(controller, lambda p, e: SourceApproval('docs', plan, 'policy'))
    assert discovery.propose('source', {'product_id': 'widget', 'version': 'unknown',
                                      'candidate_id': 'vendor'})['status'] == 'unresolved'
    assert controller.plan == {}


def test_scoped_product_tokens_preserved(tmp_path):
    from broker.librarian_discovery import DiscoveryController
    from broker.librarian_agent_client import validate_arguments
    proposal = {'product_id': '@vendor/widget', 'version': '1.2.3', 'candidate_id': 'vendor-docs'}
    calls = []
    discovery = DiscoveryController(AcquisitionController('run', tmp_path, {}),
                                    lambda p, e: calls.append(p))
    validate_arguments('source', proposal)
    discovery.propose('source', proposal)
    assert calls == [proposal]


def test_component_approval_retains_dependency_evidence(tmp_path, repository):
    from broker.controller_integrations import IntegratedController
    from broker.librarian_discovery import DiscoveryController, SourceApproval
    repo, pin = repository
    plan = {'kind': 'git', 'repository': str(repo), 'commit': pin,
            'documentation_roots': ['Documentation']}
    controller = IntegratedController('run', tmp_path / 'corpus', {'parent': plan})
    assert controller.acquire('parent')['status'] == 'complete'
    def policy(proposal, evidence):
        assert evidence['sources']['parent']['occurrences']
        assert proposal['evidence_source_ids'] == ['parent']
        return SourceApproval('dependency-docs', plan, 'verified-dependency-policy')
    discovery = DiscoveryController(controller, policy)
    proposal = {'product_id': 'dependency', 'version': 'unknown', 'dependency_of': 'widget',
                'evidence_source_ids': ['parent']}
    result = discovery.propose('component', proposal)
    assert result['status'] == 'approved'
    receipt = controller.evidence().snapshot()['discovery']['receipts'][result['receipt_id']]
    assert receipt['evidence_source_ids'] == ['parent']
    assert discovery.catalog()['components'] == [{**proposal, 'source_ids': ['dependency-docs']}]
    assert controller.acquire('dependency-docs')['status'] == 'complete'


@pytest.mark.parametrize('value', ['../secret', 'https://evil.example', '/etc/passwd'])
def test_candidate_is_not_a_network_or_path_capability(tmp_path, value):
    from broker.librarian_discovery import DiscoveryController
    from broker.librarian_agent_client import validate_arguments
    proposal = {'product_id': 'widget', 'version': 'unknown', 'candidate_id': value}
    discovery = DiscoveryController(AcquisitionController('run', tmp_path, {}),
                                    lambda p, e: pytest.fail('policy must not run'))
    with pytest.raises(ValueError):
        validate_arguments('source', proposal)
    with pytest.raises(ValueError):
        discovery.propose('source', proposal)


