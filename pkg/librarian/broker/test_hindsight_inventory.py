import json
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from broker.hindsight_inventory import HindsightInventoryReader
from broker.transport import HindsightHTTPTransport


class FakeTransport:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if "/documents/" in request.path:
            document_id = request.path.rsplit("/", 1)[-1]
            bank_id = unquote(request.path.split("/banks/", 1)[1].split("/documents/", 1)[0])
            return type("Response", (), {"status": 200, "body": json.dumps({
                "id": document_id, "bank_id": bank_id, "status": "complete",
            }).encode()})()
        offset = int(request.path.split("offset=")[1])
        body = self.pages.get(offset, {"items": [], "total": 0, "limit": 100, "offset": offset})
        return type("Response", (), {"status": 200, "body": json.dumps(body).encode()})()


@contextmanager
def paginated_server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            query = dict(part.split("=") for part in self.path.split("?", 1)[1].split("&"))
            offset = int(query["offset"])
            items = [{"id": f"doc-{index}", "product": "p", "version": str(index),
                      "source": "s", "sha256": "a" * 64, "status": "complete"}
                     for index in range(offset, min(offset + 100, 101))]
            payload = {"items": items, "total": 101, "limit": 100, "offset": offset}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_inventory_paginates_through_real_hardened_transport():
    with paginated_server() as (base, seen):
        result = HindsightInventoryReader(
            HindsightHTTPTransport(base, "test-secret"), "Liv", detail_reads=0
        ).inventory()

    assert result.complete is True
    assert len(tuple(result.documents)) == 101
    assert [path for path, _ in seen] == [
        "/v1/default/banks/Liv/documents?limit=100&offset=0",
        "/v1/default/banks/Liv/documents?limit=100&offset=100",
    ]
    assert all(auth == "Bearer test-secret" for _, auth in seen)


def test_reader_paginates_and_only_ledger_backed_documents_are_verified():
    digest = "a" * 64
    transport = FakeTransport({
        0: {"items": [{"id": "one", "product": "p", "version": "1", "source": "s", "sha256": digest}] + [{"id": f"f{i}", "product": "p", "version": "1", "source": "s", "sha256": digest} for i in range(99)], "total": 101, "limit": 100, "offset": 0},
        100: {"items": [{"id": "two", "product": "p", "version": "2", "source": "s", "sha256": digest}], "total": 101, "limit": 100, "offset": 100},
    })
    result = HindsightInventoryReader(transport, "bank/quoted", ledger={
        "claims": {"x": {"state": "completed", "caller": "liv", "bank": "bank/quoted", "document_id": "one", "product": "p", "version": "1", "source": "s", "sha256": digest}}
    }, caller="liv", detail_reads=101).inventory()
    assert result.complete is True
    docs = tuple(result.documents)
    assert len(docs) == 101
    assert docs[0].verified is True
    assert docs[1].verified is False
    assert "%2F" in transport.requests[0].path


def test_duplicate_page_is_incomplete_and_does_not_prove_match():
    transport = FakeTransport({
        0: {"items": [{"id": "one", "product": "p"}], "total": 101, "limit": 1, "offset": 0},
        1: {"items": [{"id": "one", "product": "p"}], "total": 101, "limit": 1, "offset": 1},
    })
    result = HindsightInventoryReader(transport, "Liv").inventory()
    assert result.complete is False
    assert result.has_more is True


def test_missing_hash_remains_unresolved_not_fabricated():
    transport = FakeTransport({0: {"items": [{"id": "one", "product": "p", "version": "1", "source": "s"}], "total": 1, "limit": 100, "offset": 0}})
    result = HindsightInventoryReader(transport, "Liv").inventory()
    assert result.complete is False
    docs = tuple(result.documents)
    assert docs[0].content_hash == ""
    assert docs[0].verified is False


def test_retained_context_artifact_digest_is_the_document_identity():
    """Hindsight's content_hash covers the payload; the context names the artifact."""
    artifact = "a" * 64
    payload = "b" * 64
    row = {
        "id": "file_deadbeef", "bank_id": "Liv", "content_hash": payload,
        "retain_params": {"context": json.dumps({
            "artifact_sha256": artifact, "bank": "Liv", "caller": "default",
            "product": "github-cli", "source_sha256": ["c" * 64],
            "provenance": [{"id": "x", "sha256": "d" * 64}],
        })},
    }
    transport = FakeTransport({0: {"items": [row], "total": 1, "limit": 100, "offset": 0}})
    result = HindsightInventoryReader(transport, "Liv").inventory()
    documents = tuple(result.documents)
    assert documents[0].content_hash == artifact
    assert documents[0].bank == "Liv"


def test_context_source_digests_are_never_used_as_the_artifact_digest():
    """A source or provenance digest in the context is not the submitted payload."""
    payload = "b" * 64
    row = {
        "id": "file_deadbeef", "bank_id": "Liv", "content_hash": payload,
        "retain_params": {"context": json.dumps({
            "sha256": "e" * 64, "source_sha256": ["c" * 64],
            "provenance": [{"id": "x", "sha256": "d" * 64}],
        })},
    }
    transport = FakeTransport({0: {"items": [row], "total": 1, "limit": 100, "offset": 0}})
    result = HindsightInventoryReader(transport, "Liv").inventory()
    assert tuple(result.documents)[0].content_hash == payload


def test_non_json_context_is_ignored_without_error():
    row = {"id": "file_deadbeef", "bank_id": "Liv", "content_hash": "b" * 64,
           "retain_params": {"context": "conversation between Hermes Agent and the User"}}
    transport = FakeTransport({0: {"items": [row], "total": 1, "limit": 100, "offset": 0}})
    result = HindsightInventoryReader(transport, "Liv").inventory()
    assert tuple(result.documents)[0].content_hash == "b" * 64


def test_inventory_context_digest_proves_reuse_with_a_completed_claim():
    """End-to-end: the retained context digest plus a terminal claim is reuse."""
    from broker.corpus_preflight import (
        BankDocument, BankReaderResult, CorpusKey, PreflightDisposition, assess_reuse)

    artifact = "a" * 64
    row = {"id": "file_deadbeef", "bank_id": "Liv",
           "retain_params": {"context": json.dumps({"artifact_sha256": artifact})}}
    transport = FakeTransport({0: {"items": [row], "total": 1, "limit": 100, "offset": 0}})
    reader = HindsightInventoryReader(transport, "Liv", caller="liv")
    # Mirror the helper's gate: normalise the scanned rows and keep the
    # checkpoint's coverage verdict, exactly as tools/librarian-ingest.py does.
    item = reader._normalise(row)
    documents = [BankDocument(item.product or "", item.version or "", item.source or "",
                              item.content_hash or "", True, "Liv")]

    def bank_reader(_requested):
        return BankReaderResult(tuple(documents), complete=True)

    ledger = {"claims": {"x": {"state": "completed", "caller": "liv", "bank": "Liv",
                               "sha256": artifact}}}
    key = CorpusKey("liv", "Liv", "github-cli", "2.93.0", "https://cli.github.com/manual/", artifact)
    assert assess_reuse(key, bank_reader=bank_reader, ledger=ledger).disposition \
        is PreflightDisposition.VERIFIED_PRESENT
    # The same shape without a terminal claim must not be treated as reuse.
    assert assess_reuse(key, bank_reader=bank_reader, ledger={}).disposition \
        is PreflightDisposition.UNKNOWN


def test_transport_error_is_sanitized():
    class Broken:
        def __call__(self, request):
            raise RuntimeError("Bearer super-secret")
    result = HindsightInventoryReader(Broken(), "Liv").inventory()
    assert result.complete is False
    assert all("super-secret" not in repr(doc) for doc in result.documents)


def test_identity_lookup_distinguishes_verified_404_from_transport_failure():
    class StatusTransport:
        def __init__(self, status): self.status = status
        def __call__(self, request):
            return type("Response", (), {"status": self.status, "body": b"{}"})()

    assert HindsightInventoryReader(StatusTransport(404), "Liv").lookup_document(
        document_id="known-id").status == "missing"
    assert HindsightInventoryReader(StatusTransport(403), "Liv").lookup_document(
        document_id="known-id").status == "unknown"


def test_checkpoint_advances_offset_and_reuses_prior_pages():
    transport = FakeTransport({
        0: {"items": [{"id": "one"}], "total": 2, "limit": 100, "offset": 0},
        1: {"items": [{"id": "two"}], "total": 2, "limit": 100, "offset": 1},
    })
    with tempfile.NamedTemporaryFile() as state:
        reader = HindsightInventoryReader(transport, "Liv")
        first = reader.inventory_checkpoint(state.name, ticks=1)
        second = reader.inventory_checkpoint(state.name, ticks=1)
    assert first.complete is False and first.offset == 1
    assert second.complete is True and second.offset == 2
    assert [request.path for request in transport.requests] == [
        "/v1/default/banks/Liv/documents?limit=100&offset=0",
        "/v1/default/banks/Liv/documents?limit=100&offset=1",
    ]


def test_checkpoint_terminal_short_page_restarts_once_and_stays_incomplete(tmp_path):
    transport = FakeTransport({
        0: {"items": [{"id": "one"}], "total": 2, "limit": 100, "offset": 0},
    })
    path = tmp_path / "checkpoint.json"
    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(path, ticks=3)
    assert result.complete is False
    assert result.reason == "restart_required"
    assert result.offset == 0
    # One bounded restart; no terminal empty-page loop.
    assert len(transport.requests) == 4


def test_checkpoint_empty_page_at_terminal_offset_restarts_with_prior_evidence(tmp_path):
    transport = FakeTransport({
        3: {"items": [], "total": 3, "limit": 100, "offset": 3},
        0: {"items": [{"id": "replacement"}], "total": 1, "limit": 100, "offset": 0},
    })
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({"bank": "Liv", "total": 3, "offset": 3,
                                "documents": [{"id": "one"}, {"id": "two"}],
                                "complete": False}))

    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(path, ticks=1)

    assert result.complete is True
    assert result.reason == ""
    assert result.offset == result.total == 1
    assert result.prior_documents == ({"id": "one"}, {"id": "two"})
    assert [request.path for request in transport.requests] == [
        "/v1/default/banks/Liv/documents?limit=100&offset=3",
        "/v1/default/banks/Liv/documents?limit=100&offset=0",
    ]


def test_checkpoint_duplicate_ids_in_one_page_is_not_complete(tmp_path):
    transport = FakeTransport({
        0: {"items": [{"id": "one"}, {"id": "one"}], "total": 2, "limit": 100, "offset": 0},
    })
    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(
        tmp_path / "checkpoint.json", ticks=1)
    assert result.complete is False
    assert result.reason == "restart_required"


def test_inventory_missing_ids_are_unknown_not_complete():
    transport = FakeTransport({
        0: {"items": [{"product": "p"}, {"product": "q"}], "total": 2, "limit": 100, "offset": 0},
    })
    result = HindsightInventoryReader(transport, "Liv").inventory()
    assert result.complete is False
    assert result.has_more is True


def test_inventory_duplicate_ids_in_one_page_are_unknown_not_complete():
    transport = FakeTransport({
        0: {"items": [{"id": "one"}, {"id": "one"}], "total": 2, "limit": 100, "offset": 0},
    })
    result = HindsightInventoryReader(transport, "Liv").inventory()
    assert result.complete is False
    assert result.has_more is True


def test_checkpoint_total_drift_preserves_old_evidence_and_restarts(tmp_path):
    class Drifting(FakeTransport):
        def __init__(self):
            super().__init__({})
            self.calls = 0

        def __call__(self, request):
            self.requests.append(request)
            self.calls += 1
            offset = int(request.path.split("offset=")[1])
            total = 2 if self.calls == 1 else 3
            return type("Response", (), {"status": 200, "body": json.dumps({
                "items": [{"id": "one"}] if offset == 0 else [],
                "total": total, "limit": 100, "offset": offset,
            }).encode()})()

    transport = Drifting()
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({"bank": "Liv", "total": 2, "offset": 1,
                                "documents": [{"id": "old"}], "complete": False}))
    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(path, ticks=1)
    assert result.complete is False
    assert result.reason == "restart_required"
    saved = json.loads(path.read_text())
    assert saved["prior_documents"] == [{"id": "old"}]


def test_checkpoint_missing_ids_cannot_prove_complete(tmp_path):
    transport = FakeTransport({
        0: {"items": [{"id": "one"}], "total": 2, "limit": 100, "offset": 0},
        1: {"items": [], "total": 2, "limit": 100, "offset": 1},
    })
    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(
        tmp_path / "checkpoint.json", ticks=2)
    assert result.complete is False
    assert result.reason == "restart_required"
    assert len(result.documents) == 1


def test_scan_tolerates_append_only_growth_and_reports_it(tmp_path):
    """A bank that grows mid-pass still yields a complete snapshot claim."""
    transport = FakeTransport({
        0: {"items": [{"id": "one"}], "total": 2, "limit": 100, "offset": 0},
        1: {"items": [{"id": "two"}], "total": 3, "limit": 100, "offset": 1},
    })
    path = tmp_path / "checkpoint.json"
    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(path, ticks=2)

    assert result.complete is True
    assert result.reason == ""
    assert result.total == 2 and result.offset == 2
    assert [row["id"] for row in result.documents] == ["one", "two"]
    assert result.snapshot_grew is True
    saved = json.loads(path.read_text())
    assert saved["complete"] is True and saved["snapshot_grew"] is True


def test_scan_restarts_when_the_bank_shrinks(tmp_path):
    """Shrinkage breaks the append-only premise, so the pass restarts."""
    transport = FakeTransport({
        0: {"items": [{"id": "one"}], "total": 3, "limit": 100, "offset": 0},
        1: {"items": [{"id": "two"}], "total": 2, "limit": 100, "offset": 1},
    })
    path = tmp_path / "checkpoint.json"
    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(path, ticks=2)

    assert result.complete is False
    assert result.reason == "restart_required"
    assert result.snapshot_grew is False
    assert json.loads(path.read_text())["complete"] is False


def test_checkpoint_empty_fresh_bank_is_complete(tmp_path):
    transport = FakeTransport({
        0: {"items": [], "total": 0, "limit": 100, "offset": 0},
    })
    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(
        tmp_path / "checkpoint.json", ticks=1)
    assert result.complete is True
    assert result.total == 0
    assert result.offset == 0
    assert result.documents == ()


def test_cached_complete_checkpoint_requires_freshness(tmp_path):
    transport = FakeTransport({
        0: {"items": [{"id": "new"}], "total": 1, "limit": 100, "offset": 0},
    })
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({"bank": "Liv", "total": 1, "offset": 1,
                                "documents": [{"id": "old"}], "complete": True}))
    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(path, ticks=1)
    assert result.complete is False
    assert result.reason == "restart_required"
    assert len(transport.requests) == 1


def test_cached_complete_checkpoint_rejects_duplicate_identity_page(tmp_path):
    transport = FakeTransport({
        0: {"items": [{"id": "same"}, {"id": "same"}], "total": 2,
            "limit": 100, "offset": 0},
    })
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({
        "bank": "Liv", "total": 2, "offset": 2,
        "documents": [{"id": "same"}, {"id": "same"}], "complete": True,
    }))

    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(path, ticks=1)

    assert result.complete is False
    assert result.reason == "restart_required"


def test_checkpoint_full_stable_restart_can_complete(tmp_path):
    transport = FakeTransport({
        0: {"items": [{"id": "new-one"}], "total": 2, "limit": 100, "offset": 0},
        1: {"items": [{"id": "new-two"}], "total": 2, "limit": 100, "offset": 1},
    })
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({
        "bank": "Liv", "total": None, "offset": 0, "documents": [],
        "complete": False, "reason": "restart_required",
        "prior_documents": [{"id": "old-one"}],
    }))

    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(path, ticks=2)

    assert result.complete is True
    assert result.reason == ""
    assert result.offset == result.total == 2
    assert [row["id"] for row in result.documents] == ["new-one", "new-two"]
    assert result.prior_documents == ({"id": "old-one"},)


def test_filename_lookup_does_not_assume_filename_is_document_id():
    transport = FakeTransport({0: {"items": [{"id": "actual-id", "filename": "prepared.md"}],
                                  "total": 1, "limit": 100, "offset": 0}})
    result = HindsightInventoryReader(transport, "Liv").lookup_document(filename="prepared.md")
    assert result.status == "present"
    assert result.document_id == "actual-id"


def test_empty_checkpoint_is_not_an_eternal_absence_proof(tmp_path):
    """A bank that was empty on first scan must not keep reading as empty."""
    transport = FakeTransport({
        0: {"items": [{"id": "first-doc"}], "total": 1, "limit": 100, "offset": 0},
    })
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({"bank": "Liv", "total": 0, "offset": 0, "documents": [],
                                "complete": True}))
    reader = HindsightInventoryReader(transport, "Liv")

    first = reader.inventory_checkpoint(path, ticks=1)
    assert first.complete is False
    assert first.reason == "restart_required"
    assert first.documents == ()
    # The stale absence proof must not survive on disk as a complete empty scan.
    assert json.loads(path.read_text())["complete"] is False

    second = reader.inventory_checkpoint(path, ticks=1)
    assert second.complete is True
    assert second.reason == ""
    assert second.total == 1
    assert [row["id"] for row in second.documents] == ["first-doc"]


def test_truly_empty_bank_still_proves_absence_after_the_freshness_read(tmp_path):
    transport = FakeTransport({})
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({"bank": "Liv", "total": 0, "offset": 0, "documents": [],
                                "complete": True}))
    result = HindsightInventoryReader(transport, "Liv").inventory_checkpoint(path, ticks=1)

    assert result.complete is True
    assert (result.total, result.offset, result.documents) == (0, 0, ())
    # Exactly the freshness read: absence is still provable, just not for free.
    assert len(transport.requests) == 1


def test_retained_context_identity_fields_are_recovered_and_conflicts_refuse():
    """Product/version in the retained context must be visible to the reuse check."""
    from broker.corpus_preflight import (
        BankDocument, BankReaderResult, CorpusKey, PreflightDisposition, assess_reuse)

    artifact = "b" * 64
    row = {"id": "file_cafebabe", "bank_id": "Liv",
           "retain_params": {"context": json.dumps(
               {"artifact_sha256": artifact, "bank": "Liv", "caller": "default",
                "product": "jq", "version": "1.8.1"})}}
    transport = FakeTransport({0: {"items": [row], "total": 1, "limit": 100, "offset": 0}})
    reader = HindsightInventoryReader(transport, "Liv", caller="default")

    item = reader._normalise(row)
    assert (item.product, item.version, item.content_hash) == ("jq", "1.8.1", artifact)
    assert item.source is None

    def documents_for(observed):
        # Mirrors the helper's gate: a row without an explicit source is not
        # "verified", so reuse rests on the terminal claim plus exact bytes.
        return BankReaderResult((BankDocument(observed.product or "", observed.version or "",
                                              observed.source or "", observed.content_hash or "",
                                              False, "Liv"),), complete=True)

    ledger = {"claims": {"x": {"state": "completed", "caller": "default", "bank": "Liv",
                               "sha256": artifact}}}
    key = CorpusKey("default", "Liv", "jq", "1.8.1",
                    "https://raw.githubusercontent.com/jqlang/jq/jq-1.8.1/COPYING", artifact)
    assert assess_reuse(key, bank_reader=lambda _requested: documents_for(item),
                        ledger=ledger).disposition is PreflightDisposition.VERIFIED_PRESENT

    conflicting = reader._normalise({"retain_params": {"context": json.dumps(
        {"artifact_sha256": artifact, "product": "oniguruma", "version": "6.9.10"})}})
    verdict = assess_reuse(key, bank_reader=lambda _requested: documents_for(conflicting),
                           ledger=ledger)
    assert verdict.disposition is not PreflightDisposition.VERIFIED_PRESENT
    assert verdict.disposition is PreflightDisposition.UNKNOWN
