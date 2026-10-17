import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from broker.corpus_preflight import (
    BankDocument,
    BankReaderResult,
    CorpusKey,
    PreflightDisposition,
    RunClaimStore,
    assess_reuse,
)


def key(**overrides):
    values = dict(caller="liv", bank="Liv", product="widget", version="1", source="docs", content_hash="a" * 64)
    values.update(overrides)
    return CorpusKey(**values)


def test_verified_bank_document_wins_over_completed_operation():
    result = assess_reuse(
        key(),
        bank_reader=lambda _: BankReaderResult(
            documents=[BankDocument(product="widget", version="1", source="docs", content_hash="a" * 64, verified=True, bank="Liv")],
            complete=True,
        ),
        ledger={"claims": {"x": {"state": "completed", "operation_ids": ["op"]}}},
    )
    assert result.disposition is PreflightDisposition.VERIFIED_PRESENT


def test_completed_operation_without_verified_bank_document_is_not_proof():
    result = assess_reuse(
        key(),
        bank_reader=lambda _: BankReaderResult(documents=[], complete=True),
        ledger={"claims": {"x": {"state": "completed", "caller": "liv", "bank": "Liv", "sha256": "a" * 64, "operation_ids": ["op"]}}},
    )
    assert result.disposition is PreflightDisposition.UNKNOWN


def test_completed_claim_plus_exact_bank_document_proves_reuse():
    result = assess_reuse(
        key(),
        bank_reader=lambda _: BankReaderResult(documents=[
            BankDocument(product="widget", version="1", source="docs",
                         content_hash="a" * 64, verified=False, bank="Liv"),
        ], complete=True),
        ledger={"claims": {"x": {
            "state": "completed", "caller": "liv", "bank": "Liv",
            "sha256": "a" * 64, "product": "widget", "version": "1",
        }}},
    )
    assert result.disposition is PreflightDisposition.VERIFIED_PRESENT


def test_pending_ledger_is_ingestion_pending():
    result = assess_reuse(
        key(),
        bank_reader=lambda _: BankReaderResult(documents=[], complete=True),
        ledger={"claims": {"x": {"state": "claimed", "caller": "liv", "bank": "Liv", "sha256": "a" * 64, "product": "widget", "version": "1", "source": "docs"}}},
    )
    assert result.disposition is PreflightDisposition.INGESTION_PENDING


def completed_claim(**overrides):
    claim = {"state": "completed", "caller": "liv", "bank": "Liv", "sha256": "a" * 64,
             "product": "widget", "version": "1"}
    claim.update(overrides)
    return {"claims": {"x": claim}}


def hash_only_document(digest="a" * 64, bank="Liv", **metadata):
    """A retained bank row that exposes only the content hash, as Hindsight does."""
    return BankDocument(metadata.pop("product", ""), metadata.pop("version", ""),
                        metadata.pop("source", ""), digest, metadata.pop("verified", False), bank)


def test_completed_claim_plus_bank_content_hash_without_metadata_proves_reuse():
    """The real bank shape: retained bytes carry no corpus product/version/source."""
    result = assess_reuse(
        key(),
        bank_reader=lambda _: BankReaderResult([hash_only_document()], True),
        ledger=completed_claim(),
    )
    assert result.disposition is PreflightDisposition.VERIFIED_PRESENT


def test_bank_content_hash_identity_is_case_insensitive():
    result = assess_reuse(
        key(content_hash="A" * 64),
        bank_reader=lambda _: BankReaderResult([hash_only_document("a" * 64)], True),
        ledger=completed_claim(sha256="A" * 64),
    )
    assert result.disposition is PreflightDisposition.VERIFIED_PRESENT


def test_bank_content_without_any_claim_is_unresolved_not_missing():
    """Exact bytes already in the bank must never be re-posted as new content."""
    result = assess_reuse(
        key(),
        bank_reader=lambda _: BankReaderResult([hash_only_document()], True),
        ledger={},
    )
    assert result.disposition is PreflightDisposition.UNKNOWN
    assert "durable claim" in result.reason


def test_bank_content_claimed_by_another_caller_is_unresolved():
    result = assess_reuse(
        key(),
        bank_reader=lambda _: BankReaderResult([hash_only_document()], True),
        ledger=completed_claim(caller="other"),
    )
    assert result.disposition is PreflightDisposition.UNKNOWN


def test_conflicting_exposed_metadata_refuses_hash_identity():
    """A bank row that does expose metadata must agree with the requested key."""
    document = hash_only_document(product="other-widget")
    result = assess_reuse(
        key(),
        bank_reader=lambda _: BankReaderResult([document], True),
        ledger=completed_claim(),
    )
    assert result.disposition is PreflightDisposition.UNKNOWN
    assert result.reason == "matching claim requires reconciliation"


def test_bank_content_in_another_bank_does_not_satisfy_the_claim():
    """Hash identity is bank-scoped; another bank's copy cannot prove this one."""
    result = assess_reuse(
        key(),
        bank_reader=lambda _: BankReaderResult([hash_only_document(bank="Other")], True),
        ledger=completed_claim(),
    )
    assert result.disposition is PreflightDisposition.UNKNOWN
    assert result.reason == "matching claim requires reconciliation"


def test_unrelated_bank_content_and_unrelated_claim_is_missing():
    """A hash that only appears elsewhere in the bank is genuinely absent."""
    result = assess_reuse(
        key(),
        bank_reader=lambda _: BankReaderResult([hash_only_document("b" * 64)], True),
        ledger=completed_claim(sha256="b" * 64),
    )
    assert result.disposition is PreflightDisposition.MISSING


def test_pending_claim_with_bank_content_stays_ingestion_pending():
    result = assess_reuse(
        key(),
        bank_reader=lambda _: BankReaderResult([hash_only_document()], True),
        ledger={"claims": {"x": {"state": "pending", "caller": "liv", "bank": "Liv", "sha256": "a" * 64}}},
    )
    assert result.disposition is PreflightDisposition.INGESTION_PENDING


@pytest.mark.parametrize("reader", [
    lambda _: BankReaderResult(documents=[], complete=False),
    lambda _: (_ for _ in ()).throw(RuntimeError("reader failed")),
])
def test_incomplete_or_failed_reader_fails_closed(reader):
    assert assess_reuse(key(), bank_reader=reader, ledger={}).disposition is PreflightDisposition.UNKNOWN


def test_matching_isolated_by_bank_and_hash():
    docs = [BankDocument(product="widget", version="1", source="docs", content_hash="a" * 64, verified=True, bank="Liv")]
    assert assess_reuse(key(bank="Other"), bank_reader=lambda _: BankReaderResult(docs, True), ledger={}).disposition is PreflightDisposition.MISSING
    assert assess_reuse(key(content_hash="b" * 64), bank_reader=lambda _: BankReaderResult(docs, True), ledger={}).disposition is PreflightDisposition.MISSING


def test_unknown_failed_matching_claims_are_unknown_but_unrelated_completed_is_missing():
    for state in ("unknown", "failed"):
        result = assess_reuse(key(), bank_reader=lambda _: BankReaderResult([], True), ledger={
            "claims": {"x": {"state": state, "caller": "liv", "bank": "Liv", "sha256": "a" * 64}}
        })
        assert result.disposition is PreflightDisposition.UNKNOWN
    result = assess_reuse(key(), bank_reader=lambda _: BankReaderResult([], True), ledger={
        "claims": {"x": {"state": "completed", "caller": "other", "bank": "Liv", "sha256": "a" * 64}}
    })
    assert result.disposition is PreflightDisposition.MISSING


def test_claim_scope_and_unscoped_documents_do_not_match():
    result = assess_reuse(key(), bank_reader=lambda _: BankReaderResult(
        [BankDocument("widget", "1", "docs", "a" * 64, True)], True),
        ledger={"claims": {"x": {"state": "claimed", "bank": "Liv", "sha256": "a" * 64}}})
    assert result.disposition is PreflightDisposition.MISSING


def test_reader_error_is_sanitized():
    result = assess_reuse(key(), bank_reader=lambda _: (_ for _ in ()).throw(RuntimeError("secret")), ledger={})
    assert result.reason == "bank reader failed"


@pytest.mark.parametrize("field", ["caller", "bank", "product", "version", "source", "content_hash"])
def test_corpus_key_requires_valid_fields(field):
    values = dict(caller="liv", bank="Liv", product="widget", version="1", source="docs", content_hash="a" * 64)
    values[field] = "" if field != "content_hash" else "not-sha256"
    with pytest.raises(ValueError):
        CorpusKey(**values)


def test_partial_pagination_is_unknown_even_if_page_has_match():
    result = assess_reuse(key(), bank_reader=lambda _: BankReaderResult(
        [BankDocument("widget", "1", "docs", "a" * 64, True, "Liv")], complete=False, has_more=True
    ), ledger={})
    assert result.disposition is PreflightDisposition.UNKNOWN


def test_same_claim_concurrency_has_one_winner(tmp_path):
    store = RunClaimStore(tmp_path / "claims.sqlite")
    args = dict(namespace="caller:bank", product="widget", version="1", scope_fingerprint="scope", lease_owner="worker")
    barrier = threading.Barrier(8)
    def claim(i):
        barrier.wait()
        return store.claim(**args, run_id=f"run-{i}", now=100.0, lease_seconds=60)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))
    assert sum(r.status == "claimed" for r in results) == 1
    assert sum(r.status == "attach-existing" for r in results) == 7


def test_claims_are_isolated_and_expiry_requires_recovery(tmp_path):
    store = RunClaimStore(tmp_path / "claims.sqlite")
    first = store.claim("ns", "p", "1", "a", "owner", "run-a", 100, 10)
    other = store.claim("ns", "p", "1", "b", "owner", "run-b", 100, 10)
    expired = store.claim("ns", "p", "1", "a", "owner-2", "run-c", 111, 10)
    recovered = store.claim("ns", "p", "1", "a", "owner-2", "run-c", 111, 10, recover_expired=True)
    assert first.status == "claimed"
    assert other.status == "claimed"
    assert expired.status == "recovery-required"
    assert recovered.status == "claimed"


def test_lease_validation_and_fenced_renew_release(tmp_path):
    store = RunClaimStore(tmp_path / "claims.sqlite")
    with pytest.raises(ValueError):
        store.claim("ns", "p", "1", "a", "owner", "run", 0, 0)
    assert store.claim("ns", "p", "1", "a", "owner", "run", 100, 10).status == "claimed"
    assert not store.renew("ns", "p", "1", "a", "other", "run", 101, 10)
    assert not store.release("ns", "p", "1", "a", "owner", "other")
    assert store.renew("ns", "p", "1", "a", "owner", "run", 101, 10)
    assert store.release("ns", "p", "1", "a", "owner", "run")
