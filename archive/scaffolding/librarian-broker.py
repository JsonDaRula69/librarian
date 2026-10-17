#!/usr/bin/env python3
"""Safe Librarian broker client; lease values stay inside this process."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from broker.client import BrokerClient
from broker.core import Binding


def client_from_env(args) -> BrokerClient:
    required = {
        'LIBRARIAN_BROKER_URL': os.environ.get('LIBRARIAN_BROKER_URL', ''),
        'LIBRARIAN_LEASE_ID': os.environ.get('LIBRARIAN_LEASE_ID', ''),
        'LIBRARIAN_LEASE_SECRET': os.environ.get('LIBRARIAN_LEASE_SECRET', ''),
    }
    if not all(required.values()):
        raise SystemExit('broker lease environment unavailable')
    binding = Binding(args.caller_profile, args.workspace, args.corpus_root, args.hindsight_bank)
    return BrokerClient(
        required['LIBRARIAN_BROKER_URL'], required['LIBRARIAN_LEASE_ID'],
        required['LIBRARIAN_LEASE_SECRET'], binding,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='Use the caller-scoped Librarian broker')
    parser.add_argument('--caller-profile', required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--corpus-root', required=True)
    parser.add_argument('--hindsight-bank', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('bank-list')
    status = sub.add_parser('operation-status')
    status.add_argument('operation_id')
    recall = sub.add_parser('recall')
    recall.add_argument('query')
    retain = sub.add_parser('retain-file')
    retain.add_argument('path')
    retain.add_argument('--context', required=True)
    retain.add_argument('--tag', action='append', default=[])
    args = parser.parse_args()
    client = client_from_env(args)
    if args.command == 'bank-list':
        response = client.bank_list()
    elif args.command == 'operation-status':
        response = client.operation_status(args.operation_id)
    elif args.command == 'recall':
        response = client.recall({'query': args.query, 'budget': 'low', 'max_tokens': 1024})
    else:
        path = Path(args.path).resolve()
        if not path.is_file() or path.suffix.lower() != '.md':
            raise SystemExit('retain-file requires an existing .md file')
        boundary = '----LibrarianBroker' + uuid.uuid4().hex
        content = path.read_bytes()
        request = json.dumps({'files_metadata': [{'context': args.context, 'tags': args.tag}]})
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{path.name}"\r\n'
            'Content-Type: text/markdown\r\n\r\n'
        ).encode() + content + (
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="request"\r\n'
            f'Content-Type: application/json\r\n\r\n{request}\r\n--{boundary}--\r\n'
        ).encode()
        response = client.retain(body, f'multipart/form-data; boundary={boundary}')
    try:
        body = json.loads(response.body)
    except json.JSONDecodeError:
        body = {'error': 'non-JSON broker response'}
    print(json.dumps({'status': response.status, 'body': body}, ensure_ascii=True))
    if response.status >= 400:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
