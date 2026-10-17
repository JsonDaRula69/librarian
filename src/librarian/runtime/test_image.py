"""Explicit live Docker tests; no inference service or production controller."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from broker.librarian_isolation import IsolationConfig, run_isolated
from broker.librarian_relay import ControllerAdapter, InferenceProxy, RelaySession
from broker.librarian_runtime import run_relay

HERE = Path(__file__).resolve().parent
IMAGE = json.loads((HERE / 'image.json').read_text())['image_id']
ARGV = ['/usr/local/bin/python', '-I', '-m', 'broker.librarian_agent_client']
REQUEST = {'schema_version': 1, 'products': [{'id': 'docs', 'version': 'unknown'}]}


def docker(*args):
    return subprocess.check_output(['docker', *args], timeout=15)


def test_files_and_live_denials():
    lock = json.loads((HERE / 'inputs.lock.json').read_text())
    script = r'''
import errno, hashlib, json, os, pathlib, socket
assert os.getuid() == os.getgid() == 65534
assert set(os.environ) <= {'PATH','HOME','HERMES_HOME','TMPDIR','PYTHONDONTWRITEBYTECODE','LC_CTYPE'}
assert not any(pathlib.Path('/scratch').iterdir())
assert not pathlib.Path('/home/jsondarula').exists()
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/root').exists()
assert not pathlib.Path('/bin/sh').exists()
assert sorted(p.name for p in pathlib.Path('/sys/class/net').iterdir()) == ['lo']
for target in ('/usr/local/denied', '/etc/denied'):
    try: pathlib.Path(target).write_text('denied')
    except OSError: pass
    else: raise AssertionError('root writable')
s = socket.socket(); s.settimeout(.2)
try: s.connect(('192.0.2.1', 443))
except OSError: pass
else: raise AssertionError('network allowed')
pathlib.Path('/scratch/canary').write_text('ephemeral')
status = pathlib.Path('/proc/self/status').read_text()
assert 'CapEff:\t0000000000000000' in status
assert 'NoNewPrivs:\t1' in status
assert 'Seccomp:\t2' in status
files = {}
for root in ('/usr', '/lib', '/lib64'):
    for p in pathlib.Path(root).rglob('*'):
        if p.is_file(): files[str(p).lstrip('/')] = hashlib.sha256(p.read_bytes()).hexdigest()
print(json.dumps(files))
'''
    result = run_isolated(IsolationConfig(IMAGE), ['/usr/local/bin/python', '-I', '-c', script], timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {p: v['sha256'] for p, v in lock['files'].items()}
    names = [p for p in lock['files'] if '/broker/' in p]
    assert {Path(p).name for p in names} == {'__init__.py', 'librarian_agent_client.py', 'librarian_request.py', 'librarian_identity.py'}
    init = next(p for p in names if p.endswith('__init__.py'))
    assert lock['files'][init]['sha256'] == hashlib.sha256(b'').hexdigest()


def exercise(decisions, expected_code, expected_calls):
    calls, prompts, inspections = [], [], []
    artifact = 'a' * 64

    class Controller:
        plan = {'docs': {}}
        def acquire(self, source_id):
            calls.append(['acquire', source_id])
            return {'status': 'complete'}

    class Ingestion:
        def prepare(self, source_id):
            calls.append(['prepare', source_id]); return [{'id': artifact}]
        def submit(self, artifact_id):
            calls.append(['submit', artifact_id]); return {'status': 'accepted'}
        def poll(self, artifact_id):
            calls.append(['poll', artifact_id]); return {'status': 'completed'}

    def inference(**kwargs):
        prompts.append(json.loads(kwargs['prompt']))
        if len(prompts) == 1:
            ids = docker('ps', '-q', '--filter', 'ancestor=' + IMAGE).decode().split()
            assert len(ids) == 1
            info = json.loads(docker('inspect', ids[0]))[0]
            config = info['HostConfig']
            assert config['NetworkMode'] == 'none' and config['ReadonlyRootfs']
            assert not config.get('Binds') and not config.get('Devices')
            assert not config['Privileged'] and config['CapDrop'] == ['ALL']
            assert all(m['Type'] == 'tmpfs' for m in info['Mounts'])
            assert info['Config']['User'] == '65534:65534'
            inspections.append(info['Name'].lstrip('/'))
        return json.dumps(decisions[len(prompts) - 1])

    session = RelaySession(ControllerAdapter(Controller(), Ingestion()),
                           inference=InferenceProxy('fixture-only-route', inference))
    result = run_relay(IsolationConfig(IMAGE), ARGV, session, request=REQUEST, timeout=30)
    assert result.returncode == expected_code, result.stderr
    if expected_code == 0:
        assert result.stderr == b''
    else:
        assert result.stderr.endswith(b'librarian client failed\n')
        assert len(result.stderr) < 4096
    assert calls == expected_calls
    assert len(prompts) == len(decisions)
    assert inspections == [result.container_name]
    assert not docker('ps', '-aq', '--filter', 'name=^/' + result.container_name + '$').strip()
    assert session.closed
    return result


def test_actual_client_round_trip():
    artifact = 'a' * 64
    decisions = [
        {'tool': 'source_list', 'arguments': {}},
        {'tool': 'source_acquire', 'arguments': {'source_id': 'docs'}},
        {'tool': 'source_prepare', 'arguments': {'source_id': 'docs'}},
        {'tool': 'artifact_submit', 'arguments': {'artifact_id': artifact}},
        {'tool': 'artifact_poll', 'arguments': {'artifact_id': artifact}},
        {'tool': 'discovery_status', 'arguments': {}},
        {'final': 'Fixture complete; not production evidence.'},
    ]
    exercise(decisions, 0, [['acquire', 'docs'], ['prepare', 'docs'],
                            ['submit', artifact], ['poll', artifact]])


def test_actual_client_denies_unadvertised_tool():
    exercise([{'tool': 'terminal', 'arguments': {'command': 'id'}}], 1, [])


def test_actual_client_denies_unknown_source():
    exercise([{'tool': 'source_acquire', 'arguments': {'source_id': 'not-approved'}}], 1, [])
