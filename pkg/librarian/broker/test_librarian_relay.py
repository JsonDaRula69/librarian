import json

from broker import librarian_relay as relay


class Controller:
    plan = {'docs': {'repository': '/private/repo'}}

    def __init__(self):
        self.calls = []

    def acquire(self, source):
        self.calls.append(source)
        return {'status': 'complete', 'path': '/private/repo', 'secret': 'token'}


def request(op, params, identifier=1):
    return json.dumps({'id': identifier, 'op': op, 'params': params}).encode() + b'\n'


def test_acquisition_uses_controller_plan_and_projects_response():
    controller = Controller()
    server = relay.RelaySession(relay.ControllerAdapter(controller))
    result = json.loads(server.feed(request('source.acquire', {'source_id': 'docs'})))
    assert result == {'id': 1, 'ok': True, 'result': {'status': 'complete'}}
    assert controller.calls == ['docs']
    for params in ({'source_id': 'unknown'}, {'source_id': '../docs'},
                   {'source_id': 'docs', 'url': 'https://evil.test'},
                   {'source_id': 'docs', 'code': 'print(1)'}):
        assert not json.loads(server.feed(request('source.acquire', params)))['ok']
    assert controller.calls == ['docs']
