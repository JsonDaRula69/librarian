import hashlib

from broker.controller_integrations import validate_ingestion
from broker.document_payload_policy import ASSET_ONLY, NEEDS_EXTRACTION, classify_payload
from broker.test_controller_integrations import FakeUpstream, ingestion_factory, repository


def test_pdf_remains_pending_without_text_extraction():
    result = classify_payload(b"%PDF-1.7\nopaque", path="manual.pdf")
    assert result.classification == NEEDS_EXTRACTION
    assert result.reason == "pdf-requires-text-extraction"
    assert result.sha256 == hashlib.sha256(b"%PDF-1.7\nopaque").hexdigest()


def test_classification_tamper_is_rejected_after_ledger_restart(ingestion_factory):
    ingestion = ingestion_factory(FakeUpstream(), acquire=True)
    artifact = ingestion.prepare("docs")[0]
    source = ingestion.controller._state["sources"]["docs"]
    occurrence = next(o for o in source["occurrences"] if o["id"] in artifact["occurrence_ids"])
    ingestion.state["classifications"][occurrence["id"]]["sha256"] = "0" * 64
    with ingestion._ledger_lock():
        ingestion._save()
    restarted = ingestion_factory(FakeUpstream())
    contract = {"run_id": "run", "corpus_root": str(restarted.controller.root), "hindsight_bank": "Liv"}
    errors = validate_ingestion(restarted.controller.evidence(), contract, [artifact])
    assert restarted.state["classifications"][occurrence["id"]]["sha256"] != "0" * 64
    assert errors
