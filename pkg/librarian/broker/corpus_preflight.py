"""Side-effect-free corpus reuse preflight and atomic launcher run claims."""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


class PreflightDisposition(str, Enum):
    VERIFIED_PRESENT = "verified-present"
    INGESTION_PENDING = "ingestion-pending"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CorpusKey:
    caller: str
    bank: str
    product: str
    version: str
    source: str
    content_hash: str

    def __post_init__(self) -> None:
        for name in ("caller", "bank", "product", "version", "source", "content_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.content_hash):
            raise ValueError("content_hash must be a SHA-256 hex digest")


@dataclass(frozen=True)
class BankDocument:
    product: str
    version: str
    source: str
    content_hash: str
    verified: bool = False
    bank: str | None = None


@dataclass(frozen=True)
class BankReaderResult:
    documents: Iterable[BankDocument]
    complete: bool
    has_more: bool = False


@dataclass(frozen=True)
class PreflightResult:
    disposition: PreflightDisposition
    reason: str


def _same_hash(observed: str | None, requested: str) -> bool:
    """SHA-256 identity is case-insensitive; empty or missing never matches."""
    return bool(observed) and observed.lower() == requested.lower()


def _matches(document: BankDocument, requested: CorpusKey) -> bool:
    return (
        document.product == requested.product
        and document.version == requested.version
        and document.source == requested.source
        and _same_hash(document.content_hash, requested.content_hash)
        and document.bank == requested.bank
    )


def _content_identity_matches(document: BankDocument, requested: CorpusKey) -> bool:
    """Byte-level reuse identity from the bank's own document listing.

    The bound bank retains prepared bytes but does not reproduce the caller's
    corpus product/version/source on every document row.  Those fields are
    therefore compared only when the bank actually exposes them: an exposed
    value that conflicts still refuses the match.  Bank scope and the exact
    SHA-256 of the prepared bytes are always required.
    """
    if document.bank != requested.bank:
        return False
    if not _same_hash(document.content_hash, requested.content_hash):
        return False
    for field in ("product", "version", "source"):
        observed = getattr(document, field)
        if observed and observed != getattr(requested, field):
            return False
    return True


def _ledger_claims(ledger: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    claims = ledger.get("claims", {})
    if isinstance(claims, Mapping):
        return (claim for claim in claims.values() if isinstance(claim, Mapping))
    if isinstance(claims, list):
        return (claim for claim in claims if isinstance(claim, Mapping))
    return ()


def _claim_matches(claim: Mapping[str, Any], requested: CorpusKey) -> bool:
    # Claims need explicit caller and bank scope; missing scope is not trusted.
    if claim.get("caller") != requested.caller or claim.get("bank") != requested.bank:
        return False
    if claim.get("sha256", claim.get("content_hash")) != requested.content_hash:
        return False
    for field in ("product", "version", "source"):
        if field in claim and claim[field] != getattr(requested, field):
            return False
    return True


def assess_reuse(
    requested: CorpusKey,
    *,
    bank_reader: Callable[[CorpusKey], BankReaderResult],
    ledger: Mapping[str, Any],
) -> PreflightResult:
    """Read bank first, then local metadata; never treats recall or an op as proof."""
    try:
        observed = bank_reader(requested)
        if not observed.complete or observed.has_more:
            return PreflightResult(PreflightDisposition.UNKNOWN, "bank pagination is incomplete")
        documents = tuple(observed.documents)
    except Exception:
        return PreflightResult(PreflightDisposition.UNKNOWN, "bank reader failed")

    if any(document.verified and _matches(document, requested) for document in documents):
        return PreflightResult(PreflightDisposition.VERIFIED_PRESENT, "verified bank document matches")

    for claim in _ledger_claims(ledger):
        state = str(claim.get("state", "")).lower()
        if not _claim_matches(claim, requested):
            continue
        if state in {"completed", "complete"} and any(
                _content_identity_matches(document, requested) for document in documents):
            # Bank bytes at the exact prepared hash plus a terminal, caller- and
            # bank-scoped durable claim is reuse, not a new submission.
            return PreflightResult(
                PreflightDisposition.VERIFIED_PRESENT,
                "exact bank content hash and completed durable claim match",
            )
        if state in {
            "claimed", "submitted", "pending", "processing", "ingestion-pending",
        }:
            return PreflightResult(PreflightDisposition.INGESTION_PENDING, "matching ledger claim is not complete")
        if state in {"completed", "complete", "unknown", "failed"}:
            return PreflightResult(PreflightDisposition.UNKNOWN, "matching claim requires reconciliation")

    if any(_content_identity_matches(document, requested) for document in documents):
        # The bank already holds these exact bytes, but nothing local proves who
        # submitted them or that the write reached terminal status.  Re-posting
        # would duplicate content, so the disposition stays unresolved.
        return PreflightResult(
            PreflightDisposition.UNKNOWN,
            "bank content matches prepared bytes without a corroborating durable claim",
        )

    return PreflightResult(PreflightDisposition.MISSING, "no verified document or active claim")


@dataclass(frozen=True)
class ClaimResult:
    status: str
    run_id: str
    existing_run_id: str | None = None


class RunClaimStore:
    """SQLite-backed atomic claims. Expired claims are never silently stolen."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._init_lock = threading.Lock()
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS run_claims (
                    namespace TEXT NOT NULL, product TEXT NOT NULL, version TEXT NOT NULL,
                    scope_fingerprint TEXT NOT NULL, lease_owner TEXT NOT NULL,
                    run_id TEXT NOT NULL, expires_at REAL NOT NULL,
                    PRIMARY KEY (namespace, product, version, scope_fingerprint)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def claim(
        self, namespace: str, product: str, version: str, scope_fingerprint: str,
        lease_owner: str, run_id: str, now: float, lease_seconds: float,
        *, recover_expired: bool = False,
    ) -> ClaimResult:
        _validate_claim_args(namespace, product, version, scope_fingerprint, lease_owner, run_id, now, lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT run_id, expires_at FROM run_claims WHERE namespace=? AND product=? AND version=? AND scope_fingerprint=?",
                (namespace, product, version, scope_fingerprint),
            ).fetchone()
            if row:
                existing_run_id, expires_at = row
                if expires_at > now:
                    connection.execute("COMMIT")
                    return ClaimResult("attach-existing", run_id, existing_run_id)
                if not recover_expired:
                    connection.execute("COMMIT")
                    return ClaimResult("recovery-required", run_id, existing_run_id)
            connection.execute(
                "INSERT OR REPLACE INTO run_claims VALUES (?, ?, ?, ?, ?, ?, ?)",
                (namespace, product, version, scope_fingerprint, lease_owner, run_id, now + lease_seconds),
            )
            connection.execute("COMMIT")
            return ClaimResult("claimed", run_id)

    def renew(self, namespace: str, product: str, version: str, scope_fingerprint: str,
              lease_owner: str, run_id: str, now: float, lease_seconds: float) -> bool:
        _validate_claim_args(namespace, product, version, scope_fingerprint, lease_owner, run_id, now, lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE run_claims SET expires_at=? WHERE namespace=? AND product=? AND version=?
                   AND scope_fingerprint=? AND lease_owner=? AND run_id=? AND expires_at>?""",
                (now + lease_seconds, namespace, product, version, scope_fingerprint, lease_owner, run_id, now),
            ).rowcount
            connection.execute("COMMIT")
            return changed == 1


    def release(self, namespace: str, product: str, version: str, scope_fingerprint: str,
                lease_owner: str, run_id: str) -> bool:
        _validate_claim_strings(namespace, product, version, scope_fingerprint, lease_owner, run_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """DELETE FROM run_claims WHERE namespace=? AND product=? AND version=?
                   AND scope_fingerprint=? AND lease_owner=? AND run_id=?""",
                (namespace, product, version, scope_fingerprint, lease_owner, run_id),
            ).rowcount
            connection.execute("COMMIT")
            return changed == 1


def _validate_claim_strings(*values: str) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("claim values must be non-empty strings")


def _validate_claim_args(namespace: str, product: str, version: str, scope_fingerprint: str,
                         lease_owner: str, run_id: str, now: float, lease_seconds: float) -> None:
    _validate_claim_strings(namespace, product, version, scope_fingerprint, lease_owner, run_id)
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now must be finite")
    if not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be finite and positive")


def scope_fingerprint(*parts: str) -> str:
    """Stable helper for launcher-defined scope (does not normalize caller values)."""
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
