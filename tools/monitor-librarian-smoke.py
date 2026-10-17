#!/usr/bin/env python3
"""Finish the broker smoke verification when its Hindsight operation settles."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
import sys


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    try:
        return Path.home() / ".hermes"
    except (RuntimeError, OSError):
        return Path(".hermes")


ROOT = _hermes_home().parent / 'Liv-Home'
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from broker.core import Binding, LeaseBroker
from broker.proxy import HindsightProxy
from broker.transport import HindsightHTTPTransport

BANK = 'Liv-Librarian-Smoke'
OPERATION_ID = 'f11430ca-e2db-4915-a1e0-f400a60d8448'
RUN = ROOT / 'Doc Corpus/reports/runs/librarian-ingest-smoke-20260827'
RESULT = RUN / 'result.json'
LEDGER = ROOT / 'Doc Corpus/dedupe/librarian-smoke-ledger.json'
ENV = _hermes_home() / '.env'
TERMINAL = {'completed', 'failed', 'cancelled', 'not_found'}


def key() -> str:
    for line in ENV.read_text(encoding='utf-8-sig').splitlines():
        if line.startswith('HINDSIGHT_API_KEY='):
            value = line.split('=', 1)[1].strip().strip('"\'')
            if value:
                return value
    # The plaintext copy now lives in Infisical; resolve it through this profile's
    # read-only identity before failing.
    import subprocess
    resolved = subprocess.run([str(_hermes_home() / 'bin' / 'liv-secret'), 'HINDSIGHT_API_KEY'],
                              capture_output=True, text=True, timeout=15)
    if resolved.returncode == 0 and resolved.stdout.strip():
        return resolved.stdout.strip()
    raise RuntimeError('Hindsight credential unavailable')


def direct_status(api_key: str) -> dict:
    url = f'http://127.0.0.1:8888/v1/default/banks/{BANK}/operations/{OPERATION_ID}'
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def finish(api_key: str, status: dict) -> None:
    result = json.loads(RESULT.read_text())
    result['terminal_status'] = status
    state = status.get('status')
    recall = None
    if state == 'completed':
        binding = Binding('default', str(ROOT), str(ROOT / 'Doc Corpus'), BANK)
        broker = LeaseBroker({'default': binding})
        lease = broker.issue(binding, 'librarian-ingest-smoke-recall-20260827', {'recall'}, 120)
        proxy = HindsightProxy(broker, HindsightHTTPTransport('http://127.0.0.1:8888', api_key, 30))
        body = json.dumps({'query': 'librarian-broker-smoke-20260827-v1', 'budget': 'low', 'max_tokens': 1024}).encode()
        response = proxy.forward(lease.lease_id, lease.secret, binding, 'recall', body=body, content_type='application/json')
        recall = {'status': response.status, 'body': json.loads(response.body)}
        broker.revoke(lease.lease_id, lease.secret)
    result['recall_status'] = recall
    result['verification_complete'] = state in TERMINAL
    RESULT.write_text(json.dumps(result, indent=2) + '\n')
    ledger = json.loads(LEDGER.read_text())
    for claim in ledger.get('claims', {}).values():
        if OPERATION_ID in claim.get('operation_ids', []):
            claim['state'] = state
    LEDGER.write_text(json.dumps(ledger, indent=2) + '\n')
    print(json.dumps({'operation_id': OPERATION_ID, 'state': state, 'recall_status': None if recall is None else recall['status']}))


def main() -> None:
    api_key = key()
    for _ in range(360):
        status = direct_status(api_key)
        if status.get('status') in TERMINAL:
            finish(api_key, status)
            return
        time.sleep(60)
    raise SystemExit('smoke operation still nonterminal after 6 hours')


if __name__ == '__main__':
    main()
