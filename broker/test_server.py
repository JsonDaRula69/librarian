from datetime import datetime, timezone
import json
import threading
import unittest
import urllib.error
import urllib.request

from broker.core import Binding, LeaseBroker
from broker.server import BrokerHTTPServer, Reaper


class HTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.liv = Binding('default', '/home/jsondarula/Liv-Home', '/home/jsondarula/Liv-Home/Doc Corpus', 'Liv')
        self.broker = LeaseBroker({'default': self.liv})
        self.server = BrokerHTTPServer(('127.0.0.1', 0), self.broker, 'control-test')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method='GET', body=None, control=False, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {'Content-Type': 'application/json'}
        if control:
            h['X-Broker-Control-Token'] = 'control-test'
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def binding_body(self):
        return {
            'caller_profile': self.liv.caller_profile,
            'workspace': self.liv.workspace,
            'corpus_root': self.liv.corpus_root,
            'hindsight_bank': self.liv.hindsight_bank,
        }

    def test_create_authorize_status_and_revoke(self):
        body = {**self.binding_body(), 'run_id': 'run-http', 'operations': ['retain'], 'ttl_seconds': 60}
        status, created = self.request('/v1/leases', 'POST', body, control=True)
        self.assertEqual(status, 201)
        self.assertIn('lease_secret', created)

        headers = {
            'X-Librarian-Lease-Id': created['lease_id'],
            'Authorization': f"Bearer {created['lease_secret']}",
        }
        status, result = self.request('/v1/authorize', 'POST', {**self.binding_body(), 'operation': 'retain'}, headers=headers)
        self.assertEqual((status, result['authorized']), (200, True))

        status, metadata = self.request(f"/v1/leases/{created['lease_id']}", control=True)
        self.assertEqual(status, 200)
        self.assertNotIn('lease_secret', metadata)

        status, revoked = self.request(
            f"/v1/leases/{created['lease_id']}", 'DELETE', {'lease_secret': created['lease_secret']}, control=True
        )
        self.assertEqual((status, revoked['revoked']), (200, True))
        status, _ = self.request('/v1/authorize', 'POST', {**self.binding_body(), 'operation': 'retain'}, headers=headers)
        self.assertEqual(status, 403)

    def test_control_and_cross_bank_denials(self):
        body = {**self.binding_body(), 'run_id': 'run-http', 'operations': ['retain']}
        status, _ = self.request('/v1/leases', 'POST', body)
        self.assertEqual(status, 403)
        body['hindsight_bank'] = 'gtm-expert'
        status, _ = self.request('/v1/leases', 'POST', body, control=True)
        self.assertEqual(status, 403)

    def test_non_loopback_binding_rejected(self):
        with self.assertRaises(ValueError):
            BrokerHTTPServer(('0.0.0.0', 0), self.broker, 'control')


if __name__ == '__main__':
    unittest.main()
