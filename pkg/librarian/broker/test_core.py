from datetime import datetime, timedelta, timezone
import unittest

from broker.core import AuthorizationError, Binding, LeaseBroker, minimal_child_env, secret_fingerprint


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 27, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class LeaseBrokerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.liv = Binding('default', '/home/jsondarula/Liv-Home', '/home/jsondarula/Liv-Home/Doc Corpus', 'Liv')
        self.gtm = Binding('gtm-expert', '/sandbox/gtm', '/sandbox/gtm/Doc Corpus', 'gtm-expert')
        self.broker = LeaseBroker({'default': self.liv, 'gtm-expert': self.gtm}, now=self.clock)

    def test_documentation_lease_may_span_six_hours(self):
        lease = self.broker.issue(
            self.liv, 'long-doc-run', {'bank_list'}, 21600,
        )
        self.assertEqual(
            int((lease.expires_at - lease.created_at).total_seconds()), 21600,
        )

    def issue(self, binding=None):
        return self.broker.issue(binding or self.liv, 'run-1', {'bank_list', 'retain', 'operation_status', 'recall'}, 60)

    def test_valid_liv_and_gtm_bindings(self):
        for binding in (self.liv, self.gtm):
            lease = self.issue(binding)
            self.assertEqual(self.broker.authorize(lease.lease_id, lease.secret, binding, 'bank_list').binding, binding)

    def test_cross_bank_and_foreign_workspace_denied(self):
        lease = self.issue()
        with self.assertRaises(AuthorizationError):
            self.broker.authorize(lease.lease_id, lease.secret, self.gtm, 'retain')
        foreign = Binding('default', '/tmp/foreign', self.liv.corpus_root, 'Liv')
        with self.assertRaises(AuthorizationError):
            self.broker.authorize(lease.lease_id, lease.secret, foreign, 'retain')

    def test_expiry_revoke_replay_and_operation_allowlist(self):
        lease = self.issue()
        with self.assertRaises(AuthorizationError):
            self.broker.authorize(lease.lease_id, lease.secret, self.liv, 'delete_bank')
        self.clock.value += timedelta(seconds=61)
        with self.assertRaises(AuthorizationError):
            self.broker.authorize(lease.lease_id, lease.secret, self.liv, 'retain')
        self.assertEqual(self.broker.reap(), 1)
        lease = self.issue()
        self.assertTrue(self.broker.revoke(lease.lease_id, lease.secret))
        self.assertFalse(self.broker.revoke(lease.lease_id, lease.secret))
        with self.assertRaises(AuthorizationError):
            self.broker.authorize(lease.lease_id, lease.secret, self.liv, 'retain')
        with self.assertRaises(AuthorizationError):
            self.broker.authorize(lease.lease_id, 'wrong', self.liv, 'retain')

    def test_secret_is_not_in_metadata_and_child_env_is_minimal(self):
        lease = self.issue()
        metadata = self.broker.metadata(lease.lease_id)
        self.assertNotIn(lease.secret, repr(metadata))
        env = minimal_child_env({'PATH': '/bin', 'HOME': '/home/user', 'HINDSIGHT_API_KEY': 'static', 'AWS_SECRET_ACCESS_KEY': 'secret'}, lease.secret)
        self.assertEqual(set(env), {'PATH', 'HOME', 'LIBRARIAN_LEASE_SECRET'})
        self.assertEqual(env['LIBRARIAN_LEASE_SECRET'], lease.secret)
        self.assertNotIn('static', repr(env))
        self.assertNotIn('secret', repr(env))

    def test_rejects_path_traversal_and_missing_fields(self):
        with self.assertRaises(AuthorizationError):
            self.broker.issue(Binding('default', '/home/jsondarula/Liv-Home/../other', self.liv.corpus_root, 'Liv'), 'run', {'retain'})
        with self.assertRaises(AuthorizationError):
            self.broker.issue(self.liv, '', {'retain'})


if __name__ == '__main__':
    unittest.main()
