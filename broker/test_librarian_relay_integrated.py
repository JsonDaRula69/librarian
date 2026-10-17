"""Offline exercise of the actual controller adapters through JSON-line RPC."""
import json

from broker.controller_integrations import IntegratedController, ControllerIngestion
from broker.librarian_relay import ControllerAdapter, RelaySession
from broker.test_controller_integrations import repository, FakeUpstream
from broker.test_librarian_relay import request


def test_real_controller_acquisition_preparation_and_ingestion(tmp_path, repository):
    repo, pin = repository
    controller = IntegratedController('relay-test', tmp_path / 'corpus', {'docs': {
        'kind': 'git', 'repository': str(repo), 'commit': pin,
        'documentation_roots': ['Documentation']}})
    upstream = FakeUpstream()
    ingestion = ControllerIngestion(controller, 'Liv', upstream)
    session = RelaySession(ControllerAdapter(controller, ingestion))
    responses = []

    def call(op, params):
        raw = session.feed(request(op, params))
        responses.append(raw)
        result = json.loads(raw)
        assert result['ok'], result
        return result['result']

    assert call('source.acquire', {'source_id': 'docs'}) == {'status': 'complete'}
    ids = call('source.prepare', {'source_id': 'docs'})['artifact_ids']
    assert len(ids) == 2
    asset_occurrence = next(row for row in controller.evidence().snapshot()['sources']['docs']['occurrences']
                            if row['path'].endswith('logo.bin'))
    assert asset_occurrence['id'] not in {
        occurrence_id for identifier in ids
        for occurrence_id in ingestion.state['artifacts'][identifier]['occurrence_ids']
    }
    for identifier in ids:
        assert call('artifact.submit', {'artifact_id': identifier}) == {'status': 'accepted'}
        assert call('artifact.poll', {'artifact_id': identifier}) == {'status': 'completed'}
    assert len(upstream.requests) == 4
    assert all(r['status'] == 'completed' for r in ingestion.state['receipts'].values())
    wire = b''.join(responses)
    assert str(tmp_path).encode() not in wire
    assert b'operation_id' not in wire and b'repository' not in wire and b'bank' not in wire
