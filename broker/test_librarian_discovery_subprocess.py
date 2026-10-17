"""Actual Hermes client and relay pipes, local Git only, no production calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from broker.controller_integrations import IntegratedController, ControllerIngestion
from broker.librarian_discovery import DiscoveryController, SourceApproval
from broker.librarian_relay import ControllerAdapter, RelaySession, InferenceProxy, serve_connection
from broker.test_controller_integrations import repository


def _hermes_runtime_available() -> bool:
    """True when the Hermes install exposes its own top-level modules in-process.

    The editable install maps ``tools``/``agent``/``utils`` but not
    ``hermes_yaml``, so ``client_tools`` -> ``tools.registry`` ->
    ``agent.secret_scope`` -> ``from utils import ...`` -> ``import hermes_yaml``
    raises ModuleNotFoundError here.  The real CLI puts the package root on
    sys.path and imports fine, so this guard re-enables itself once the
    editable install maps the module.
    """
    import importlib

    try:
        importlib.import_module('hermes_yaml')
    except Exception:
        return False
    return True


_RUNTIME_TESTS = {'test_discovery_client_subprocess_acquire_prepare'}


@pytest.fixture(autouse=True)
def _skip_without_hermes_runtime(request):
    if request.node.name.split('[')[0] in _RUNTIME_TESTS and not _hermes_runtime_available():
        pytest.skip('Hermes editable install does not expose hermes_yaml')


@pytest.mark.parametrize('approve', [True, False])
def test_discovery_client_subprocess_acquire_prepare(tmp_path, repository, approve):
    import agent.transports.chat_completions as transport
    repo, pin = repository
    plan = {'kind': 'git', 'repository': str(repo), 'commit': pin,
            'documentation_roots': ['Documentation']}
    controller = IntegratedController('discovery-e2e', tmp_path / 'corpus', {})
    def no_network(*args):
        pytest.fail('network/retain must not be called')
    ingestion = ControllerIngestion(controller, 'Liv', no_network)
    observed = []
    def policy(proposal, evidence):
        observed.append(proposal)
        if approve:
            return SourceApproval('new-docs', plan, 'fixture-policy')
    discovery = DiscoveryController(controller, policy)
    decisions = [
        {'tool': 'source_list', 'arguments': {}},
        {'tool': 'source_propose', 'arguments': {
            'product_id': 'widget', 'version': 'unknown', 'candidate_id': 'vendor-docs'}},
    ]
    if approve:
        decisions.extend([
            {'tool': 'source_list', 'arguments': {}},
            {'tool': 'source_acquire', 'arguments': {'source_id': 'new-docs'}},
            {'tool': 'source_prepare', 'arguments': {'source_id': 'new-docs'}},
        ])
    decisions.append({'final': 'done'})
    prompts = []
    def inference(**kwargs):
        prompts.append(json.loads(kwargs['prompt']))
        return json.dumps(decisions[len(prompts) - 1])
    session = RelaySession(ControllerAdapter(controller, ingestion, discovery=discovery),
                           inference=InferenceProxy('offline-route', inference))
    root = Path(__file__).resolve().parents[1]
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
        proc.stdin.write(json.dumps({'schema_version': 1, 'products': [
            {'id': 'widget', 'version': 'unknown'}]}).encode() + b'\n')
        proc.stdin.flush()
        if approve:
            serve_connection(session, proc.stdout, proc.stdin, timeout=15)
        else:
            with pytest.raises(ValueError):
                serve_connection(session, proc.stdout, proc.stdin, timeout=15)
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0, proc.stderr.read().decode()
        assert proc.stderr.read() == b''
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    assert len(observed) == 1
    assert json.loads(prompts[1]['messages'][-1]['content'])['sources'] == []
    if approve:
        assert json.loads(prompts[3]['messages'][-1]['content'])['sources'] == [{'source_id': 'new-docs'}]
        assert json.loads(prompts[-2]['messages'][-1]['content']) == {'status': 'complete'}
        ids = json.loads(prompts[-1]['messages'][-1]['content'])['artifact_ids']
        assert set(ids) == set(ingestion.state['artifacts'])
        assert len(ids) == 2
        asset = next(row for source in ingestion.state['sources'].values()
                     for row in source['occurrences']
                     if row.get('path', '').endswith('logo.bin'))
        assert ingestion.state['classifications'][asset['id']]['classification'] == 'asset-only'
        assert asset['id'] not in {
            occurrence_id for artifact in ingestion.state['artifacts'].values()
            for occurrence_id in artifact['occurrence_ids']
        }
        assert controller.evidence().snapshot()['plan'] == {'new-docs': plan}
        assert discovery.completion()['status'] == 'ready'
    else:
        assert discovery.completion()['status'] == 'blocked'
        assert controller.plan == {}
    assert str(repo) not in json.dumps(prompts)
    assert pin not in json.dumps(prompts)
