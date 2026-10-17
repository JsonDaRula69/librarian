import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from broker.core import Binding, LeaseBroker
from broker.proxy import HindsightProxy, UpstreamResponse
from broker.server import BrokerHTTPServer


class FakeTransport:
    def __init__(self):
        self.paths = []

    def __call__(self, request):
        self.paths.append(request.path)
        return UpstreamResponse(200, b'{"banks":[{"bank_id":"Liv"}]}')


class BrokerProxyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.binding = Binding('default', '/home/jsondarula/Liv-Home', '/home/jsondarula/Liv-Home/Doc Corpus', 'Liv')
        self.broker = LeaseBroker({'default': self.binding})
        self.transport = FakeTransport()
        self.proxy = HindsightProxy(self.broker, self.transport)
        self.server = BrokerHTTPServer(('127.0.0.1', 0), self.broker, 'control', self.proxy)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.lease = self.broker.issue(self.binding, 'run-http-proxy', {'bank_list'}, 60)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call_banks(self, binding=None):
        binding = binding or self.binding
        query = urllib.parse.urlencode({
            'caller_profile': binding.caller_profile,
            'workspace': binding.workspace,
            'corpus_root': binding.corpus_root,
            'hindsight_bank': binding.hindsight_bank,
        })
        req = urllib.request.Request(
            self.base + '/v1/hindsight/banks?' + query,
            headers={
                'X-Librarian-Lease-Id': self.lease.lease_id,
                'Authorization': 'Bearer ' + self.lease.secret,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_bank_list_flows_through_lease_bound_proxy(self):
        status, body = self.call_banks()
        self.assertEqual(status, 200)
        self.assertEqual(body['banks'][0]['bank_id'], 'Liv')
        self.assertEqual(self.transport.paths, ['/v1/default/banks'])

    def test_cross_bank_binding_never_reaches_transport(self):
        status, _ = self.call_banks(Binding('default', self.binding.workspace, self.binding.corpus_root, 'gtm-expert'))
        self.assertEqual(status, 403)
        self.assertEqual(self.transport.paths, [])

    def test_revoked_lease_denied(self):
        self.broker.revoke(self.lease.lease_id, self.lease.secret)
        status, _ = self.call_banks()
        self.assertEqual(status, 403)
        self.assertEqual(self.transport.paths, [])


if __name__ == '__main__':
    unittest.main()
