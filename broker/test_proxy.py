import json
import unittest

from broker.core import AuthorizationError, Binding, LeaseBroker
from broker.proxy import HindsightProxy, ProxyPolicy, UpstreamResponse


class RecordingTransport:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return UpstreamResponse(200, b'{"ok":true}')


class ProxyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.liv = Binding('default', '/home/jsondarula/Liv-Home', '/home/jsondarula/Liv-Home/Doc Corpus', 'Liv')
        self.gtm = Binding('gtm-expert', '/sandbox/gtm', '/sandbox/gtm/Doc Corpus', 'gtm-expert')
        self.broker = LeaseBroker({'default': self.liv, 'gtm-expert': self.gtm})
        self.transport = RecordingTransport()
        self.proxy = HindsightProxy(self.broker, self.transport)

    def lease(self, binding=None, operations=None):
        return self.broker.issue(binding or self.liv, 'proxy-run', operations or {'bank_list', 'retain', 'operation_status', 'recall'}, 60)

    def test_fixed_routes_use_lease_bound_bank(self):
        lease = self.lease()
        self.proxy.forward(lease.lease_id, lease.secret, self.liv, 'bank_list')
        self.proxy.forward(
            lease.lease_id, lease.secret, self.liv, 'retain', body=b'multipart',
            content_type='multipart/form-data; boundary=test'
        )
        self.proxy.forward(lease.lease_id, lease.secret, self.liv, 'operation_status', operation_id='op_123')
        self.proxy.forward(
            lease.lease_id, lease.secret, self.liv, 'recall', body=json.dumps({'query': 'x'}).encode(),
            content_type='application/json'
        )
        self.assertEqual(
            [(r.method, r.path) for r in self.transport.requests],
            [
                ('GET', '/v1/default/banks'),
                ('POST', '/v1/default/banks/Liv/files/retain'),
                ('GET', '/v1/default/banks/Liv/operations/op_123'),
                ('POST', '/v1/default/banks/Liv/memories/recall'),
            ],
        )

    def test_caller_cannot_supply_arbitrary_path_or_cross_bank(self):
        lease = self.lease()
        with self.assertRaises(AuthorizationError):
            self.proxy.forward(lease.lease_id, lease.secret, self.gtm, 'retain', body=b'x', content_type='multipart/form-data')
        with self.assertRaises(AuthorizationError):
            ProxyPolicy().request('delete_bank', 'Liv')
        self.assertEqual(self.transport.requests, [])

    def test_rejects_bad_body_types_and_operation_ids(self):
        lease = self.lease()
        with self.assertRaises(AuthorizationError):
            self.proxy.forward(lease.lease_id, lease.secret, self.liv, 'retain', body=b'{}', content_type='application/json')
        with self.assertRaises(AuthorizationError):
            self.proxy.forward(lease.lease_id, lease.secret, self.liv, 'recall', body=b'{}', content_type='text/plain')
        with self.assertRaises(AuthorizationError):
            self.proxy.forward(lease.lease_id, lease.secret, self.liv, 'operation_status', operation_id='../other')
        self.assertEqual(self.transport.requests, [])

    def test_revoked_expired_or_unallowed_never_reaches_transport(self):
        lease = self.lease(operations={'bank_list'})
        with self.assertRaises(AuthorizationError):
            self.proxy.forward(lease.lease_id, lease.secret, self.liv, 'retain', body=b'x', content_type='multipart/form-data')
        self.broker.revoke(lease.lease_id, lease.secret)
        with self.assertRaises(AuthorizationError):
            self.proxy.forward(lease.lease_id, lease.secret, self.liv, 'bank_list')
        self.assertEqual(self.transport.requests, [])


if __name__ == '__main__':
    unittest.main()
