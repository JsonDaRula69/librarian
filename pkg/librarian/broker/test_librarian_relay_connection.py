import json
import subprocess
import sys

import pytest
from broker import librarian_relay as relay
from broker.test_librarian_relay import Controller, request


def test_incremental_frames_and_limits():
    server = relay.RelaySession(relay.ControllerAdapter(Controller()), max_frame_bytes=128,
                                max_requests=2, max_total_bytes=512)
    line = request('source.acquire', {'source_id': 'docs'})
    assert server.feed(line[:8]) == b''
    assert json.loads(server.feed(line[8:]))['ok']
    assert json.loads(server.feed(line))['ok']
    with pytest.raises(relay.RelayError):
        server.feed(line)
    server = relay.RelaySession(relay.ControllerAdapter(Controller()), max_frame_bytes=128)
    with pytest.raises(relay.RelayError):
        server.feed(b'x' * 129)
    with pytest.raises(relay.RelayError):
        server.feed(line)


@pytest.mark.parametrize('data', [b'{"id":1,"id":2,"op":"source.acquire","params":{"source_id":"docs"}}\n',
    b'[]\n', b'{"id":"private-token"}\n', b'NaN\n', b'\xff\n', b'[' * 1000 + b'\n'])
def test_bad_frames_do_not_leak(data):
    server = relay.RelaySession(relay.ControllerAdapter(Controller()))
    response = json.loads(server.feed(data))
    assert response == {'id': None, 'ok': False, 'error': 'request_rejected'}


def test_connection_on_real_child_pipes():
    code = ('import sys,json; '
            'print(json.dumps({"id":1,"op":"source.acquire","params":{"source_id":"docs"}}),flush=True); '
            'r=json.loads(sys.stdin.readline()); sys.exit(0 if r["ok"] else 1)')
    with subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        relay.serve_connection(relay.RelaySession(relay.ControllerAdapter(Controller())),
                               child.stdout, child.stdin, timeout=2)
        assert child.wait(timeout=2) == 0


def test_connection_deadline_and_partial_eof():
    with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
        try:
            with pytest.raises(relay.RelayError):
                relay.serve_connection(relay.RelaySession(relay.ControllerAdapter(Controller())),
                                       child.stdout, child.stdin, timeout=.05)
        finally:
            child.kill()
    server = relay.RelaySession(relay.ControllerAdapter(Controller()))
    server.feed(b'{')
    with pytest.raises(relay.RelayError):
        server.finish()


def test_artifact_operations_are_id_only_and_projected():
    class Ingestion:
        def prepare(self, source):
            return [{'id': 'a' * 64, 'path': '/private/path', 'sha256': 'b' * 64}]
        def submit(self, identifier):
            return {'status': 'accepted', 'bank': 'private-bank'}
        def poll(self, identifier):
            return {'status': 'completed', 'operation_id': 'private-op'}
    server = relay.RelaySession(relay.ControllerAdapter(Controller(), Ingestion()))
    result = json.loads(server.feed(request('source.prepare', {'source_id': 'docs'})))
    assert result['result']['artifact_ids'] == ['a' * 64]
    assert result['result']['total'] == 1
    assert result['result']['next_offset'] is None
    for op, status in [('artifact.submit', 'accepted'), ('artifact.poll', 'completed')]:
        assert json.loads(server.feed(request(op, {'artifact_id': 'a' * 64})))['result'] == {'status': status}
        assert not json.loads(server.feed(request(op, {'artifact_id': 'c' * 64})))['ok']
        assert not json.loads(server.feed(request(op, {'artifact_id': '/etc/passwd'})))['ok']


def test_poll_before_submit_is_recoverable_then_submit_and_poll_succeeds():
    class Ingestion:
        def __init__(self):
            self.state = {'receipts': {}}
            self.calls = []

        def prepare(self, source):
            return [{'id': 'a' * 64}]

        def submit(self, identifier):
            self.calls.append(('submit', identifier))
            self.state['receipts'][identifier] = {'status': 'accepted', 'operation_id': 'op'}
            return {'status': 'accepted'}

        def poll(self, identifier):
            self.calls.append(('poll', identifier))
            return {'status': 'completed'}

    ingestion = Ingestion()
    server = relay.RelaySession(relay.ControllerAdapter(Controller(), ingestion))
    artifact = json.loads(server.feed(request('source.prepare', {'source_id': 'docs'})))['result']['artifact_ids'][0]
    response = json.loads(server.feed(request('artifact.poll', {'artifact_id': artifact})))
    assert response == {'id': 1, 'ok': False, 'error': {'code': 'submit_artifact_first'}}
    assert ingestion.calls == []
    assert json.loads(server.feed(request('artifact.submit', {'artifact_id': artifact})))['result'] == {'status': 'accepted'}
    assert json.loads(server.feed(request('artifact.poll', {'artifact_id': artifact})))['result'] == {'status': 'completed'}
    assert ingestion.calls == [('submit', artifact), ('poll', artifact)]


def test_poll_unknown_receipt_requires_reconciliation_and_never_reposts():
    class Ingestion:
        def __init__(self):
            self.state = {'receipts': {}}
            self.calls = []

        def prepare(self, source):
            return [{'id': 'a' * 64}]

        def submit(self, identifier):
            self.calls.append('submit')
            return {'status': 'accepted'}

        def poll(self, identifier):
            self.calls.append('poll')
            return {'status': 'completed'}

    ingestion = Ingestion()
    server = relay.RelaySession(relay.ControllerAdapter(Controller(), ingestion))
    artifact = json.loads(server.feed(request('source.prepare', {'source_id': 'docs'})))['result']['artifact_ids'][0]
    ingestion.state['receipts'][artifact] = {'status': 'unknown'}
    response = json.loads(server.feed(request('artifact.poll', {'artifact_id': artifact})))
    assert response == {'id': 1, 'ok': False, 'error': {'code': 'reconciliation_required'}}
    assert ingestion.calls == []


def test_large_prepare_is_cached_and_pages_without_gaps():
    class Ingestion:
        def __init__(self):
            self.calls = 0
            self.ids = [f'{i:064x}' for i in range(1001)]
        def prepare(self, source):
            self.calls += 1
            return [{'id': identifier} for identifier in self.ids]
        def resume(self):
            return {'submitted': 1, 'checked': 2, 'has_more': True,
                    'receipts': {'private': 'omitted'}}

    ingestion = Ingestion()
    server = relay.RelaySession(relay.ControllerAdapter(Controller(), ingestion))
    first = json.loads(server.feed(request('source.prepare', {'source_id': 'docs'})))
    assert first['ok'] and first['result']['artifact_ids'] == ingestion.ids[:256]
    assert first['result']['total'] == 1001
    pages = [first['result']['artifact_ids']]
    offset = first['result']['next_offset']
    while offset is not None:
        response = json.loads(server.feed(request('artifact.list', {
            'source_id': 'docs', 'offset': offset, 'limit': 137})))
        pages.append(response['result']['artifact_ids'])
        offset = response['result']['next_offset']
    assert [item for page in pages for item in page] == ingestion.ids
    assert ingestion.calls == 1

    for params in ({'source_id': 'unknown', 'offset': 0, 'limit': 1},
                   {'source_id': 'docs', 'offset': -1, 'limit': 1},
                   {'source_id': 'docs', 'offset': 0, 'limit': 0},
                   {'source_id': 'docs', 'offset': 0, 'limit': 257},
                   {'source_id': 'docs', 'offset': 0, 'limit': True}):
        assert not json.loads(server.feed(request('artifact.list', params)))['ok']

    resumed = json.loads(server.feed(request('ingestion.resume', {})))
    assert resumed == {'id': 1, 'ok': True,
                       'result': {'submitted': 1, 'checked': 2, 'has_more': True}}


def test_artifact_list_before_prepare_is_recoverable_but_foreign_source_is_not():
    server = relay.RelaySession(relay.ControllerAdapter(Controller()))
    response = json.loads(server.feed(request('artifact.list', {
        'source_id': 'docs', 'offset': 0, 'limit': 1})))
    assert response == {'id': 1, 'ok': False,
                        'error': {'code': 'prepare_source_first'}}
    assert not server.closed
    response = json.loads(server.feed(request('artifact.list', {
        'source_id': 'foreign', 'offset': 0, 'limit': 1})))
    assert response == {'id': 1, 'ok': False, 'error': 'request_rejected'}
    assert not server.closed


def test_prepare_before_acquire_is_recoverable_and_blocked_receipt_stays_blocked():
    class Ingestion:
        def prepare(self, source):
            raise AssertionError('must not prepare without receipt')

    class Controller:
        plan = {'docs': {}}
        _state = {'sources': {}}

    server = relay.RelaySession(relay.ControllerAdapter(Controller(), Ingestion()))
    response = json.loads(server.feed(request('source.prepare', {'source_id': 'docs'})))
    assert response == {'id': 1, 'ok': False,
                        'error': {'code': 'acquire_source_first'}}
    Controller._state['sources']['docs'] = {'status': 'blocked'}
    response = json.loads(server.feed(request('source.prepare', {'source_id': 'docs'})))
    assert response == {'id': 1, 'ok': False,
                        'error': {'code': 'source_acquisition_blocked'}}
