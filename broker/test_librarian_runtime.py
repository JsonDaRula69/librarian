"""Runtime tests use only temporary controller state and explicit local images."""
import importlib
import importlib.util
import hashlib
import subprocess
import time

from broker.test_controller_integrations import repository
import json
import os

import pytest

from broker.librarian_isolation import IsolationConfig
from broker.librarian_relay import ControllerAdapter, InferenceProxy, RelaySession
from broker.test_librarian_relay import Controller


REQUEST = {'schema_version': 1, 'products': [{'id': 'docs', 'version': 'unknown'}]}
IMAGE = os.environ.get('LIBRARIAN_CANARY_IMAGE')
live = pytest.mark.skipif(not IMAGE, reason='explicit immutable local canary image required')


def runtime():
    assert importlib.util.find_spec('broker.librarian_runtime'), 'duplex runtime missing'
    return importlib.import_module('broker.librarian_runtime')


def test_purpose_is_rejected_before_preflight(monkeypatch):
    module = runtime()
    def forbidden(*args):
        pytest.fail('invalid request reached Docker')
    monkeypatch.setattr(module.isolation, 'preflight', forbidden)
    for key in ('purpose', 'task', 'instructions'):
        with pytest.raises(ValueError):
            module.run_relay(IsolationConfig('sha256:' + 'a' * 64), ['/bin/true'],
                             RelaySession(ControllerAdapter(Controller())),
                             request={**REQUEST, key: 'private caller purpose'})


@live
def test_live_duplex_inference(tmp_path):
    module = runtime()
    calls = []
    def inference(**kwargs):
        calls.append(kwargs)
        return 'offline-answer'
    session = RelaySession(ControllerAdapter(Controller()), inference=InferenceProxy('fake-route', inference))
    script = '''import sys,json
request=json.loads(sys.stdin.readline())
assert request == {'schema_version': 1, 'products': [{'id': 'docs', 'version': 'unknown'}]}
print(json.dumps({'id':1,'op':'inference.complete','params':{'prompt':'document inventory'}}),flush=True)
r=json.loads(sys.stdin.readline())
assert r == {'id':1,'ok':True,'result':{'text':'offline-answer'}}, r
sys.stderr.write('duplex-ok\\n')
'''
    result = module.run_relay(IsolationConfig(IMAGE), ['/usr/local/bin/python', '-c', script],
                              session, request=REQUEST, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stderr == b'duplex-ok\n'
    assert calls == [dict(route='fake-route', prompt='document inventory', max_output_tokens=1024, timeout=30)]
    assert session.closed
    assert result.container_name.startswith('librarian-')


@live
def test_live_acquire_prepare_and_denial_canaries(tmp_path, repository, monkeypatch):
    from broker.controller_integrations import IntegratedController, ControllerIngestion
    from broker.acquisition_controller import BoundaryError
    repo, pin = repository
    controller = IntegratedController('offline-runtime', tmp_path / 'corpus', {'docs': {
        'kind': 'git', 'repository': str(repo), 'commit': pin,
        'documentation_roots': ['Documentation']}})
    requests = []
    def forbidden_transport(request):
        requests.append(request)
        raise AssertionError('no ingestion permitted')
    class PrepareOnly(ControllerIngestion):
        def submit(self, identifier):
            raise BoundaryError('offline run')
        def poll(self, identifier):
            raise BoundaryError('offline run')
    ingestion = PrepareOnly(controller, 'Liv', forbidden_transport)
    calls = []
    def fake_inference(**kwargs):
        calls.append(kwargs)
        return 'inventory-only'
    monkeypatch.setenv('LIBRARIAN_PRIVATE_CANARY', 'must-not-cross-boundary')
    script = r'''
import os,sys,json,pathlib,socket
assert 'LIBRARIAN_PRIVATE_CANARY' not in os.environ
assert os.getuid() == 65534
assert set(os.listdir('/sys/class/net')) == {'lo'}
for address in [('192.0.2.1',443), ('127.0.0.1',8888)]:
    s=socket.socket(); s.settimeout(.2)
    try: s.connect(address)
    except OSError: pass
    else: raise AssertionError('network escaped')
    finally: s.close()
for path in ['/var/run/docker.sock','/home/jsondarula/Liv-Home']:
    assert not pathlib.Path(path).exists()
for path in ['/etc/runtime-canary','/tmp/runtime-canary']:
    try: pathlib.Path(path).write_text('bad')
    except OSError: pass
    else: raise AssertionError('write escaped')
pathlib.Path('/scratch/canary').write_text('scratch-only')
status=pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status and 'Seccomp:\t2' in status
assert 'CapEff:\t0000000000000000' in status
req=json.loads(sys.stdin.readline())
assert set(req)=={'schema_version','products'}
n=0
def rpc(op,params,ok=True):
    global n
    n+=1
    wire=json.dumps({'id':n,'op':op,'params':params})+'\n'
    # Fragment frames across actual pipe writes.
    sys.stdout.write(wire[:7]); sys.stdout.flush()
    sys.stdout.write(wire[7:]); sys.stdout.flush()
    raw=sys.stdin.readline()
    assert '/home/' not in raw and 'repository' not in raw and 'bank' not in raw
    r=json.loads(raw); assert r['ok'] is ok, r
    return r.get('result')
assert rpc('source.acquire',{'source_id':'docs'}) == {'status':'complete'}
ids=rpc('source.prepare',{'source_id':'docs'})['artifact_ids']
assert len(ids)==3
assert rpc('inference.complete',{'prompt':'inventory'}) == {'text':'inventory-only'}
for op,params in [('source.acquire',{'source_id':'../secret'}),
                  ('source.acquire',{'source_id':'docs','url':'https://example.test'}),
                  ('source.prepare',{'source_id':'unknown'}),
                  ('artifact.submit',{'artifact_id':ids[0]}),
                  ('artifact.poll',{'artifact_id':ids[0]}),
                  ('artifact.submit',{'artifact_id':'/etc/passwd'}),
                  ('shell',{'command':'id'}),
                  ('inference.complete',{'prompt':'inventory','route':'other'})]:
    rpc(op,params,False)
sys.stderr.write('acquired=3 prepared=3 denials=8 os-canaries=ok\n')
'''
    session = RelaySession(ControllerAdapter(controller, ingestion),
                           inference=InferenceProxy('offline-fixed-route', fake_inference))
    result = runtime().run_relay(IsolationConfig(IMAGE), ['/usr/local/bin/python', '-c', script],
                                 session, request=REQUEST, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stderr == b'acquired=3 prepared=3 denials=8 os-canaries=ok\n'
    assert requests == [] and ingestion.state['receipts'] == {}
    assert len(calls) == 1
    evidence = controller.evidence().snapshot()
    assert evidence['sources']['docs']['status'] == 'complete'
    assert len(evidence['sources']['docs']['occurrences']) == 3
    artifacts = ingestion.state['artifacts']
    assert len(artifacts) == 3
    for artifact in artifacts.values():
        data = (controller.root / artifact['path']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == artifact['sha256']
    assert {a['representation'] for a in artifacts.values()} == {'utf-8', 'base64'}
    print(result.stderr.decode().strip(), 'image=' + IMAGE, 'container=' + result.container_name)


@live
@pytest.mark.parametrize('script,kwargs,reason', [
    ('import time; time.sleep(30)', {'timeout': .3}, 'timeout'),
    ('import os; os.write(2,b"x"*1000000)', {'max_stderr': 1024}, 'stderr limit'),
    ('import sys; sys.stdout.write("{"); sys.stdout.flush()', {}, 'relay failed'),
    ('import os; os.write(1,b"x"*1000000)', {}, 'relay failed'),
    ('import os,time; os.close(1); time.sleep(30)', {'timeout': .3}, 'timeout'),
])
def test_live_limits_cleanup(script, kwargs, reason):
    module = runtime()
    session = RelaySession(ControllerAdapter(Controller()))
    start = time.monotonic()
    with pytest.raises(module.isolation.IsolationUnavailable, match=reason):
        module.run_relay(IsolationConfig(IMAGE), ['/usr/local/bin/python', '-c', script],
                         session, request=REQUEST, **dict({'timeout': 10}, **kwargs))
    assert time.monotonic() - start < 12
    assert session.closed


@live
def test_live_watchdog_interrupts_blocked_trusted_callback():
    module = runtime()
    class Blocked:
        def acquire(self, source):
            time.sleep(30)
    session = RelaySession(Blocked())
    script = 'import sys,json; sys.stdin.readline(); print(json.dumps({"id":1,"op":"source.acquire","params":{"source_id":"docs"}}),flush=True); sys.stdin.readline()'
    start = time.monotonic()
    with pytest.raises(module.isolation.IsolationUnavailable, match='timeout'):
        module.run_relay(IsolationConfig(IMAGE), ['/usr/local/bin/python', '-c', script],
                         session, request=REQUEST, timeout=1)
    assert time.monotonic() - start < 5


def test_preflight_failure_never_starts_process(monkeypatch):
    module = runtime()
    def unavailable(config):
        raise module.isolation.IsolationUnavailable('blocked')
    monkeypatch.setattr(module.isolation, 'preflight', unavailable)
    def forbidden(*args, **kwargs):
        pytest.fail('launch after failed preflight')
    monkeypatch.setattr(module.subprocess, 'Popen', forbidden)
    with pytest.raises(module.isolation.IsolationUnavailable, match='blocked'):
        module.run_relay(IsolationConfig('sha256:' + 'a' * 64), ['/bin/true'],
                         RelaySession(ControllerAdapter(Controller())), request=REQUEST)


@pytest.mark.parametrize('kwargs', [{'timeout': float('nan')}, {'timeout': 0},
                                   {'timeout': True}, {'max_stderr': 0}])
def test_invalid_limits_before_launch(kwargs):
    with pytest.raises(ValueError):
        runtime().run_relay(IsolationConfig('sha256:' + 'a' * 64), ['/bin/true'],
                            RelaySession(ControllerAdapter(Controller())), request=REQUEST, **kwargs)


@live
def test_live_nonzero_exit_and_no_containers_left(monkeypatch):
    module = runtime()
    cleaned = []
    original = module._cleanup
    def verified_cleanup(proc, name):
        original(proc, name)
        assert proc.poll() is not None
        result = subprocess.run(module.isolation._DOCKER + [
            'ps', '-aq', '--filter', 'name=^/' + name + '$'],
            capture_output=True, check=True, timeout=10, env=module.isolation._HOST_ENV)
        assert result.stdout.strip() == b''
        cleaned.append(name)
    monkeypatch.setattr(module, '_cleanup', verified_cleanup)
    assert IMAGE is not None
    result = module.run_relay(IsolationConfig(IMAGE), ['/usr/local/bin/python', '-c', 'raise SystemExit(7)'],
                              RelaySession(ControllerAdapter(Controller())), request=REQUEST)
    assert result.returncode == 7
    assert cleaned == [result.container_name]

