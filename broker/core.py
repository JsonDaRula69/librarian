"""In-memory broker policy core for caller-scoped Librarian leases."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Mapping


class BrokerError(Exception):
    """Base broker policy error."""


class AuthorizationError(BrokerError):
    """Raised when a caller or lease is not authorized."""


@dataclass(frozen=True)
class Binding:
    caller_profile: str
    workspace: str
    corpus_root: str
    hindsight_bank: str


@dataclass
class Lease:
    lease_id: str
    secret: str
    binding: Binding
    run_id: str
    operations: frozenset[str]
    created_at: datetime
    expires_at: datetime
    revoked: bool = False


class LeaseBroker:
    """Issue and verify short-lived leases against an explicit binding map."""

    MAX_TTL_SECONDS = 21600

    def __init__(self, bindings: Mapping[str, Binding], now=None) -> None:
        self.bindings = dict(bindings)
        self._leases: dict[str, Lease] = {}
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _binding(self, requested: Binding) -> Binding:
        expected = self.bindings.get(requested.caller_profile)
        if expected is None or expected != requested:
            raise AuthorizationError("caller/workspace/corpus/bank binding rejected")
        return expected

    def issue(self, requested: Binding, run_id: str, operations: set[str], ttl_seconds: int = 1800) -> Lease:
        if (
            not run_id or not operations or ttl_seconds < 1
            or ttl_seconds > self.MAX_TTL_SECONDS
        ):
            raise AuthorizationError("invalid lease request")
        binding = self._binding(requested)
        lease = Lease(
            lease_id=secrets.token_urlsafe(18),
            secret=secrets.token_urlsafe(32),
            binding=binding,
            run_id=run_id,
            operations=frozenset(operations),
            created_at=self._now(),
            expires_at=self._now() + timedelta(seconds=ttl_seconds),
        )
        self._leases[lease.lease_id] = lease
        return lease

    def authorize(self, lease_id: str, secret: str, binding: Binding, operation: str) -> Lease:
        lease = self._leases.get(lease_id)
        if lease is None or not secrets.compare_digest(lease.secret, secret):
            raise AuthorizationError("unknown or invalid lease")
        if lease.revoked or self._now() >= lease.expires_at:
            raise AuthorizationError("expired or revoked lease")
        if lease.binding != binding:
            raise AuthorizationError("lease binding mismatch")
        if operation not in lease.operations:
            raise AuthorizationError("operation not allowed by lease")
        return lease

    def revoke(self, lease_id: str, secret: str) -> bool:
        lease = self._leases.get(lease_id)
        if lease is None or not secrets.compare_digest(lease.secret, secret):
            raise AuthorizationError("unknown or invalid lease")
        if lease.revoked:
            return False
        lease.revoked = True
        return True

    def reap(self) -> int:
        now = self._now()
        expired = [key for key, lease in self._leases.items() if lease.expires_at <= now]
        for key in expired:
            del self._leases[key]
        return len(expired)

    def metadata(self, lease_id: str) -> dict[str, object]:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise AuthorizationError("unknown lease")
        return {
            "lease_id": lease.lease_id,
            "caller_profile": lease.binding.caller_profile,
            "workspace": lease.binding.workspace,
            "corpus_root": lease.binding.corpus_root,
            "hindsight_bank": lease.binding.hindsight_bank,
            "run_id": lease.run_id,
            "operations": sorted(lease.operations),
            "created_at": lease.created_at.isoformat(),
            "expires_at": lease.expires_at.isoformat(),
            "revoked": lease.revoked,
        }


def minimal_child_env(base: Mapping[str, str], lease_secret: str) -> dict[str, str]:
    """Return only non-sensitive runtime essentials plus the leased secret."""
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "HERMES_HOME"}
    result = {key: value for key, value in base.items() if key in allowed}
    result["LIBRARIAN_LEASE_SECRET"] = lease_secret
    return result


def secret_fingerprint(secret: str) -> str:
    """Return metadata-safe evidence for tests and audit correlation."""
    return hashlib.sha256(secret.encode()).hexdigest()[:12]
