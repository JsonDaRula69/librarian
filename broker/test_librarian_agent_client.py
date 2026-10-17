"""Offline subprocess tests: real Hermes transport, fake controller inference."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import io

import pytest

from broker.librarian_relay import RelaySession, InferenceProxy
from broker.discovery_browser import DiscoveryBrowser
from broker.librarian_relay import ControllerAdapter

ROOT = Path(__file__).resolve().parents[1]
REQUEST = {'schema_version': 1, 'products': [{'id': 'docs', 'version': 'unknown'}]}


def _hermes_runtime_available() -> bool:
    """True when the Hermes install exposes its own top-level modules in-process.

    The editable install maps ``tools``/``agent``/``utils`` but not
    ``hermes_yaml``, so ``agent.secret_scope`` -> ``from utils import ...`` ->
    ``import hermes_yaml`` raises ModuleNotFoundError here, while the real CLI
    (which puts the package root on sys.path) imports fine.  These tests need
    that in-process chain and re-enable themselves once the install maps it.
    """
    try:
        importlib.import_module('hermes_yaml')
    except Exception:
        return False
    return True


_RUNTIME_TESTS = {
    'test_real_hermes_round_trip',
    'test_real_client_relay_browses_fixture_and_pages_results',
    'test_real_client_recovers_from_list_before_prepare',
    'test_real_client_acquires_after_prepare_precondition_then_completes',
    'test_model_cannot_expand_tool_surface',
    'test_discovery_only_registry_excludes_acquisition_and_ingestion_tools',
    'test_discovery_only_run_rejects_acquisition_decision',
    'test_malformed_decision_gets_one_format_only_retry',
}


@pytest.fixture(autouse=True)
def _skip_without_hermes_runtime(request):
    if request.node.name.split('[')[0] in _RUNTIME_TESTS and not _hermes_runtime_available():
        pytest.skip('Hermes editable install does not expose hermes_yaml')


def test_real_hermes_round_trip(tmp_path):
    assert importlib.util.find_spec('broker.librarian_agent_client'), 'client missing'
    calls = []

    class Controller:
        def acquire(self, source_id):
            calls.append(source_id)
            return {'status': 'complete'}

    prompts = []
    def inference(**kwargs):
        prompt = json.loads(kwargs['prompt'])
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps({'tool': 'source_acquire', 'arguments': {'source_id': 'docs'}})
        assert json.loads(prompt['messages'][-1]['content']) == {'status': 'complete'}
        return json.dumps({'final': 'Acquisition complete.'})

    session = RelaySession(Controller(), inference=InferenceProxy('offline-route', inference))
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path),
           'PYTHONPATH': os.pathsep.join([str(ROOT), str(Path(sys.executable).resolve().parents[2])]),
           'PYTHONDONTWRITEBYTECODE': '1'}
    # Resolve Hermes source, not a user's profile/config or credentials.
    import agent.transports.chat_completions as transport
    image = tmp_path / 'image'
    package = image / 'broker'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    for name in ('librarian_agent_client.py', 'librarian_request.py', 'librarian_identity.py'):
        (package / name).write_bytes((ROOT / 'broker' / name).read_bytes())
    env['PYTHONPATH'] = os.pathsep.join([str(image), str(Path(transport.__file__).parents[2])])
    proc = subprocess.Popen([sys.executable, '-m', 'broker.librarian_agent_client'],
                            cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    from broker.librarian_relay import serve_connection
    try:
        proc.stdin.write(json.dumps(REQUEST).encode() + b'\n')
        proc.stdin.flush()
        serve_connection(session, proc.stdout, proc.stdin, timeout=15)
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0, (proc.stderr.read().decode(), prompts)
        assert proc.stderr.read() == b''
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    assert calls == ['docs']
    assert len(prompts) == 2
    assert {t['function']['name'] for t in prompts[0]['tools']} == {
        'source_acquire', 'source_prepare', 'artifact_submit', 'artifact_poll',
        'source_list', 'source_propose', 'component_propose', 'discovery_status',
        'discovery_fetch', 'discovery_read', 'discovery_links',
        'discovery_register_candidate', 'artifact_list',
        'ingestion_resume'}
    assert json.loads(prompts[0]['messages'][-1]['content']) == REQUEST


def test_real_client_relay_browses_fixture_and_pages_results(tmp_path):
    """The isolated client can browse an untrusted candidate and page its receipt."""
    class Wire:
        data = (b"# Unfamiliar product\n\nA compact device guide. "
                b"[details](https://fixture.invalid/details)\nMore notes.")
        url = 'https://fixture.invalid/product'
        status = 200
        redirects = ()

    class Controller:
        plan = {}

    browser = DiscoveryBrowser(fetch=lambda *args, **kwargs: Wire())
    session = RelaySession(ControllerAdapter(Controller(), browser=browser))
    prompts = []

    def inference(**kwargs):
        prompt = json.loads(kwargs['prompt'])
        prompts.append(prompt)
        if len(prompts) == 1:
            decision = {'tool': 'discovery_fetch', 'arguments': {'url': Wire.url}}
        elif len(prompts) == 2:
            receipt = json.loads(prompt['messages'][-1]['content'])['receipt_id']
            decision = {'tool': 'discovery_read',
                        'arguments': {'receipt_id': receipt, 'offset': 0, 'limit': 12}}
        elif len(prompts) == 3:
            page = json.loads(prompt['messages'][-1]['content'])
            decision = {'tool': 'discovery_read',
                        'arguments': {'receipt_id': page['receipt_id'],
                                      'offset': page['next_offset'], 'limit': 12}}
        elif len(prompts) == 4:
            page = json.loads(prompt['messages'][-1]['content'])
            decision = {'tool': 'discovery_links', 'arguments': {'receipt_id': page['receipt_id']}}
        else:
            decision = {'final': 'Candidate evidence reviewed.'}
        return json.dumps(decision)

    session.inference = InferenceProxy('offline-route', inference)
    root = Path(__file__).resolve().parents[1]
    import agent.transports.chat_completions as transport
    image = tmp_path / 'image'
    package = image / 'broker'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    for name in ('librarian_agent_client.py', 'librarian_request.py', 'librarian_identity.py'):
        (package / name).write_bytes((root / 'broker' / name).read_bytes())
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path),
           'PYTHONPATH': os.pathsep.join([str(image), str(Path(transport.__file__).parents[2])]),
           'PYTHONDONTWRITEBYTECODE': '1'}
    proc = subprocess.Popen([sys.executable, '-m', 'broker.librarian_agent_client'],
                            cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.stdin.write(json.dumps(REQUEST).encode() + b'\n')
        proc.stdin.flush()
        from broker.librarian_relay import serve_connection
        serve_connection(session, proc.stdout, proc.stdin, timeout=15)
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0, (proc.stderr.read().decode(), prompts)
        assert proc.stderr.read() == b''
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    assert len(prompts) == 5
    assert 'fixture.invalid' not in json.dumps(prompts[0]['messages'][-1])
    assert json.loads(prompts[1]['messages'][-1]['content'])['authority'] == 'unverified_candidate'
    assert json.loads(prompts[2]['messages'][-1]['content'])['offset'] == 0
    assert json.loads(prompts[3]['messages'][-1]['content'])['offset'] == 12
    assert json.loads(prompts[4]['messages'][-1]['content'])['links'][0]['ordinal'] == 0


def test_real_client_recovers_from_list_before_prepare(tmp_path):
    class Controller:
        plan = {'docs': object()}

    class Ingestion:
        def prepare(self, source):
            return [{'id': 'a' * 64}]

    prompts = []
    def inference(**kwargs):
        prompt = json.loads(kwargs['prompt'])
        prompts.append(prompt)
        if len(prompts) == 1:
            decision = {'tool': 'artifact_list',
                        'arguments': {'source_id': 'docs', 'offset': 0, 'limit': 1}}
        elif len(prompts) == 2:
            assert json.loads(prompt['messages'][-1]['content']) == {
                'error': 'prepare_source_first', 'recoverable': True}
            decision = {'tool': 'source_prepare', 'arguments': {'source_id': 'docs'}}
        elif len(prompts) == 3:
            assert json.loads(prompt['messages'][-1]['content'])['artifact_ids'] == ['a' * 64]
            decision = {'final': 'Prepared.'}
        return json.dumps(decision)

    session = RelaySession(ControllerAdapter(Controller(), Ingestion()),
                           inference=InferenceProxy('offline-route', inference))
    root = Path(__file__).resolve().parents[1]
    import agent.transports.chat_completions as transport
    image = tmp_path / 'image'
    package = image / 'broker'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    for name in ('librarian_agent_client.py', 'librarian_request.py', 'librarian_identity.py'):
        (package / name).write_bytes((root / 'broker' / name).read_bytes())
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path),
           'PYTHONPATH': os.pathsep.join([str(image), str(Path(transport.__file__).parents[2])]),
           'PYTHONDONTWRITEBYTECODE': '1'}
    proc = subprocess.Popen([sys.executable, '-m', 'broker.librarian_agent_client'],
                            cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.stdin.write(json.dumps(REQUEST).encode() + b'\n')
        proc.stdin.flush()
        from broker.librarian_relay import serve_connection
        serve_connection(session, proc.stdout, proc.stdin, timeout=15)
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0, proc.stderr.read().decode()
        assert proc.stderr.read() == b''
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    assert len(prompts) == 3


def test_real_client_acquires_after_prepare_precondition_then_completes(tmp_path):
    class Controller:
        plan = {'docs': object()}

        def __init__(self):
            self._state = {'sources': {}}

        def acquire(self, source):
            self._state['sources'][source] = {'status': 'complete'}
            return {'status': 'complete'}

    class Ingestion:
        def prepare(self, source):
            return [{'id': 'b' * 64}]

    prompts = []
    def inference(**kwargs):
        prompt = json.loads(kwargs['prompt'])
        prompts.append(prompt)
        step = len(prompts)
        if step == 1:
            decision = {'tool': 'source_list', 'arguments': {}}
        elif step == 2:
            decision = {'tool': 'source_prepare', 'arguments': {'source_id': 'docs'}}
        elif step == 3:
            assert json.loads(prompt['messages'][-1]['content']) == {
                'error': 'acquire_source_first', 'recoverable': True}
            decision = {'tool': 'source_acquire', 'arguments': {'source_id': 'docs'}}
        elif step == 4:
            decision = {'tool': 'source_prepare', 'arguments': {'source_id': 'docs'}}
        elif step == 5:
            assert json.loads(prompt['messages'][-1]['content'])['artifact_ids'] == ['b' * 64]
            decision = {'final': 'Complete.'}
        else:
            raise AssertionError('unexpected inference call')
        return json.dumps(decision)

    controller = Controller()
    session = RelaySession(ControllerAdapter(controller, Ingestion()),
                           inference=InferenceProxy('offline-route', inference))
    root = Path(__file__).resolve().parents[1]
    import agent.transports.chat_completions as transport
    image = tmp_path / 'image'
    package = image / 'broker'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    for name in ('librarian_agent_client.py', 'librarian_request.py', 'librarian_identity.py'):
        (package / name).write_bytes((root / 'broker' / name).read_bytes())
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path),
           'PYTHONPATH': os.pathsep.join([str(image), str(Path(transport.__file__).parents[2])]),
           'PYTHONDONTWRITEBYTECODE': '1'}
    proc = subprocess.Popen([sys.executable, '-m', 'broker.librarian_agent_client'],
                            cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.stdin.write(json.dumps(REQUEST).encode() + b'\n')
        proc.stdin.flush()
        from broker.librarian_relay import serve_connection
        serve_connection(session, proc.stdout, proc.stdin, timeout=15)
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0, proc.stderr.read().decode()
        assert proc.stderr.read() == b''
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    assert len(prompts) == 5


@pytest.mark.parametrize('raw', [
    b'', b'{}', b'{"id":1,"ok":true,"result":{}}',
    b'{"id":2,"ok":true,"result":{}}\n',
    b'{"id":true,"ok":true,"result":{}}\n',
    b'{"id":1,"id":1,"ok":true,"result":{}}\n',
    b'{"id":1,"ok":false,"error":"request_rejected"}\n',
    b'{"id":1,"ok":true,"result":{},"extra":1}\n',
    b'x' * 65537 + b'\n',
])
def test_bad_response_closes_connection(raw):
    from broker.librarian_agent_client import StdioRPC, ClientError
    output = io.BytesIO()
    rpc = StdioRPC(io.BytesIO(raw), output)
    with pytest.raises(ClientError):
        rpc.call('source.acquire', {'source_id': 'docs'})
    assert rpc.closed
    sent = output.getvalue()
    with pytest.raises(ClientError):
        rpc.call('source.acquire', {'source_id': 'docs'})
    assert output.getvalue() == sent


@pytest.mark.parametrize('code', ['prepare_source_first', 'submit_artifact_first',
                                  'reconciliation_required', 'acquire_source_first',
                                  'source_acquisition_blocked'])
def test_known_recoverable_codes_stay_retryable(code):
    from broker.librarian_agent_client import StdioRPC, ClientError
    raw = json.dumps({'id': 1, 'ok': False, 'error': {'code': code}}).encode() + b'\n'
    rpc = StdioRPC(io.BytesIO(raw), io.BytesIO())
    with pytest.raises(ClientError) as exc:
        rpc.call('artifact.poll', {'artifact_id': 'a' * 64})
    assert exc.value.recoverable
    assert not rpc.closed


@pytest.mark.parametrize('key', ['purpose', 'task', 'instructions'])
def test_purpose_rejected_without_rpc(key):
    from broker.librarian_agent_client import StdioRPC, run
    output = io.BytesIO()
    rpc = StdioRPC(io.BytesIO(json.dumps({**REQUEST, key: 'private'}).encode() + b'\n'), output)
    with pytest.raises(ValueError):
        run(rpc)
    assert output.getvalue() == b''


@pytest.mark.parametrize('decision', [
    {'tool': 'terminal', 'arguments': {'command': 'id'}},
    {'tool': 'source_acquire', 'arguments': {'source_id': '../secret'}},
    {'tool': 'source_acquire', 'arguments': {'source_id': 'docs', 'url': 'https://x'}},
    {'final': 'done', 'purpose': 'hidden'},
    {'tool': [], 'arguments': {}},
])
def test_model_cannot_expand_tool_surface(decision):
    from broker.librarian_agent_client import StdioRPC, ControllerCompletionClient, controller_tools, OPERATIONS, ClientError
    raw = json.dumps({'id': 1, 'ok': True, 'result': {'text': json.dumps(decision)}}).encode() + b'\n'
    output = io.BytesIO()
    rpc = StdioRPC(io.BytesIO(raw), output)
    tools = controller_tools(rpc).get_definitions(set(OPERATIONS), quiet=True)
    with pytest.raises(ClientError):
        ControllerCompletionClient(rpc).create(messages=[], tools=tools)
    assert json.loads(output.getvalue())['op'] == 'inference.complete'


def test_discovery_only_registry_excludes_acquisition_and_ingestion_tools():
    from broker.librarian_agent_client import DISCOVERY_OPERATIONS, OPERATIONS, controller_tools
    definitions = controller_tools(io.BytesIO(), discovery_only=True).get_definitions(
        set(DISCOVERY_OPERATIONS), quiet=True)
    names = {item['function']['name'] for item in definitions}
    assert names == set(DISCOVERY_OPERATIONS)
    assert not names.intersection(set(OPERATIONS) - set(DISCOVERY_OPERATIONS))


def test_discovery_only_run_rejects_acquisition_decision():
    from broker.librarian_agent_client import ClientError, ControllerCompletionClient, StdioRPC, controller_tools
    decision = {'tool': 'source_acquire', 'arguments': {'source_id': 'docs'}}
    raw = json.dumps({'id': 1, 'ok': True, 'result': {'text': json.dumps(decision)}}).encode() + b'\n'
    rpc = StdioRPC(io.BytesIO(raw), io.BytesIO())
    tools = controller_tools(rpc, discovery_only=True).get_definitions(
        {'source_list', 'source_propose', 'component_propose', 'discovery_status',
         'discovery_fetch', 'discovery_read', 'discovery_links', 'discovery_register_candidate'}, quiet=True)
    with pytest.raises(ClientError, match='tool not exposed'):
        ControllerCompletionClient(rpc).create(messages=[], tools=tools)


def test_malformed_decision_gets_one_format_only_retry():
    from broker.librarian_agent_client import ControllerCompletionClient, StdioRPC, controller_tools, OPERATIONS
    replies = [
        {'id': 1, 'ok': True, 'result': {'text': 'not json'}},
        {'id': 2, 'ok': True, 'result': {'text': '{"final":"corrected"}'}},
    ]
    raw = b''.join(json.dumps(reply).encode() + b'\n' for reply in replies)
    output = io.BytesIO()
    rpc = StdioRPC(io.BytesIO(raw), output)
    tools = controller_tools(rpc).get_definitions(set(OPERATIONS), quiet=True)
    response = ControllerCompletionClient(rpc).create(messages=[], tools=tools)
    assert response.choices[0].message.content == 'corrected'
    prompts = output.getvalue().decode().splitlines()
    assert len(prompts) == 2
    assert 'Format correction only' in json.loads(prompts[1])['params']['prompt']


def test_inference_diagnostics_include_proxy_and_adapter_receipts():
    from broker.librarian_relay import InferenceProxy
    class Adapter:
        def __call__(self, **kwargs):
            return '{"final":"ok"}'
        def diagnostic_receipt(self):
            return {'finish_reason': 'stop', 'secret': 'redact-me'}
    proxy = InferenceProxy('route', Adapter())
    proxy.complete('{}')
    receipt = proxy.diagnostic_receipt()
    assert receipt['call_count'] == 1
    assert receipt['adapter_finish_reason'] == 'stop'
    assert 'secret' not in receipt
