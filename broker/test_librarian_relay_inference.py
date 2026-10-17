import json
from broker import librarian_relay as relay
from broker.test_librarian_relay import Controller, request


def test_pinned_inference():
    calls = []
    def infer(**kwargs):
        calls.append(kwargs)
        return 'answer'
    proxy = relay.InferenceProxy('approved-route', infer, max_calls=1)
    server = relay.RelaySession(relay.ControllerAdapter(Controller()), inference=proxy)
    assert json.loads(server.feed(request('inference.complete', {'prompt': 'hello'})))['result'] == {'text': 'answer'}
    assert calls == [{'route': 'approved-route', 'prompt': 'hello', 'max_output_tokens': 1024, 'timeout': 30}]
    assert not json.loads(server.feed(request('inference.complete', {'prompt': 'again'})))['ok']


def test_private_output_and_metadata_are_rejected():
    for output in ({'text': 'answer', 'api_key': 'secret'}, 'secret', 'approved-route', 'x' * 9):
        proxy = relay.InferenceProxy('approved-route', lambda **kw: output,
                                     private_values=('secret',), max_output_bytes=8)
        server = relay.RelaySession(relay.ControllerAdapter(Controller()), inference=proxy)
        response = server.feed(request('inference.complete', {'prompt': 'hi'}))
        assert not json.loads(response)['ok']
        assert b'secret' not in response and b'approved-route' not in response


def test_overrides_sizes_and_failed_call_budget():
    calls = []
    def infer(**kwargs):
        calls.append(kwargs)
        raise RuntimeError('api-key-private')
    proxy = relay.InferenceProxy('fixed', infer, max_calls=1, max_prompt_bytes=8)
    server = relay.RelaySession(relay.ControllerAdapter(Controller()), inference=proxy)
    for params in ({'prompt': 'hi', 'endpoint': 'https://evil'},
                   {'prompt': 'hi', 'model': 'other'}, {'prompt': 'x' * 9},
                   {'prompt': {'code': 'exec'}}):
        assert not json.loads(server.feed(request('inference.complete', params)))['ok']
    assert not calls
    for _ in range(2):
        response = server.feed(request('inference.complete', {'prompt': 'hi'}))
        assert not json.loads(response)['ok']
        assert b'api-key-private' not in response
    assert len(calls) == 1
