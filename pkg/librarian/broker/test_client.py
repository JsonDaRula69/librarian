import json
import threading
import unittest

from broker.client import BrokerClient
from broker.core import Binding, LeaseBroker
from broker.proxy import HindsightProxy, UpstreamResponse
from broker.server import BrokerHTTPServer


class RecordingTransport:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if '/operations/' in request.path:
            return UpstreamResponse(200, b'{"status":"pending"}')
        if request.path.endswith('/memories/recall'):
            return UpstreamResponse(200, b'{"results":[]}')
        if request.path.endswith('/files/retain'):
            return UpstreamResponse(200, b'{"operation_ids":["op1"]}')
        return UpstreamResponse(200, b'{"banks":[{"bank_id":"Liv"}]}')


class ClientBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.binding = Binding('default', '/home/jsondarula/Liv-Home', '/home/jsondarula/Liv-Home/Doc Corpus', 'Liv')
        self.broker = LeaseBroker({'default': self.binding})
        self.transport = RecordingTransport()
        self.proxy = HindsightProxy(self.broker, self.transport)
        self.server = BrokerHTTPServer(('127.0.0.1', 0), self.broker, 'control', self.proxy)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        lease = self.broker.issue(
            self.binding, 'client-run', {'bank_list', 'retain', 'operation_status', 'recall'}, 60
        )
        self.client = BrokerClient(
            f'http://127.0.0.1:{self.server.server_port}', lease.lease_id, lease.secret, self.binding
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_all_client_operations_use_fixed_proxy_routes(self):
        self.assertEqual(self.client.bank_list().status, 200)
        self.assertEqual(self.client.retain(b'body', 'multipart/form-data; boundary=x').status, 200)
        self.assertEqual(self.client.operation_status('op1').status, 200)
        self.assertEqual(self.client.recall({'query': 'x'}).status, 200)
        self.assertEqual(
            [request.path for request in self.transport.requests],
            [
                '/v1/default/banks',
                '/v1/default/banks/Liv/files/retain',
                '/v1/default/banks/Liv/operations/op1',
                '/v1/default/banks/Liv/memories/recall',
            ],
        )

    def test_secret_is_not_present_in_request_paths_or_bodies(self):
        secret = self.client._secret
        self.client.bank_list()
        self.client.recall({'query': 'x'})
        for request in self.transport.requests:
            self.assertNotIn(secret, request.path)
            self.assertNotIn(secret.encode(), request.body or b'')


if __name__ == '__main__':
    unittest.main()
