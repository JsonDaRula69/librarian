"""Focused offline tests for tools/librarian-ingest.py."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from broker.acquisition_controller import BoundaryError
from broker.proxy import UpstreamResponse


@pytest.fixture
def ingest_module():
    path = Path(__file__).parents[1] / "tools" / "librarian-ingest.py"
    spec = importlib.util.spec_from_file_location("librarian_ingest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(tmp_path, *, content_hash=None):
    corpus = tmp_path / "Doc Corpus"
    corpus.mkdir()
    doc = corpus / "docs" / "guide.md"
    doc.parent.mkdir()
    doc.write_text("# local guide\n", encoding="utf-8")
    asset = corpus / "docs" / "logo.png"
    asset.write_bytes(b"\x89PNG\r\n\x1a\nasset")
    rows = [{"path": "docs/guide.md", "canonical_url": "https://example.test/guide.md",
             "sha256": hashlib.sha256(doc.read_bytes()).hexdigest()},
            {"path": "docs/logo.png", "canonical_url": "https://example.test/logo.png",
             "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}]
    data = {"run_id": "offline-run", "bank": "Liv", "product": "example/docs",
            "version": "1", "source_id": "agent", "corpus_root": str(corpus),
            "documentation_roots": ["docs"], "files": rows}
    if content_hash:
        data["content_hash"] = content_hash
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, corpus


def test_prepare_classifies_local_markdown_and_asset(ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    controller, ingestion, meta, artifacts = ingest_module.prepare(
        path, workspace=tmp_path, corpus=corpus)
    assert len(artifacts) == 1
    classifications = ingestion.state["classifications"]
    assert {row["classification"] for row in classifications.values()} == {"ingestible-text", "asset-only"}
    assert controller.root == corpus.resolve()


def test_unknown_bank_gate_blocks_without_transport_write(ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path, content_hash="a" * 64)
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    controller, ingestion, meta, artifacts = ingest_module.prepare(path, workspace=tmp_path, corpus=corpus)
    calls = []

    def transport(request):
        calls.append(request)
        raise AssertionError("unexpected transport call")

    with pytest.raises(BoundaryError, match="UNKNOWN"):
        ingest_module.submit_with_gate(ingestion, artifacts[0]["id"], transport, meta, {})
    assert [request for request in calls if request.method == "POST"] == []


def test_restart_uses_durable_claim_without_duplicate_post(ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    requests = []

    def transport(request):
        requests.append(request)
        return UpstreamResponse(202, b'{"operation_ids":["op-1"]}')

    first, ingestion, meta, artifacts = ingest_module.prepare(
        path, workspace=tmp_path, corpus=corpus, transport=transport)
    receipt = ingestion.submit(artifacts[0]["id"])
    assert receipt["status"] == "accepted"
    # Re-open the same run and adapter; its existing SQLite receipt suppresses POST.
    second, resumed, _, again = ingest_module.prepare(
        path, workspace=tmp_path, corpus=corpus, transport=transport)
    assert resumed.submit(again[0]["id"])["status"] == "accepted"
    assert len([request for request in requests if request.method == "POST"]) == 1


def test_shared_parent_run_isolated_per_component_without_plan_overwrite(
        ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    first_data = json.loads(path.read_text())
    first_data.update(run_id="shared-acquisition", product="component-one", version="1")
    path.write_text(json.dumps(first_data))
    first_controller, _, first_meta, first_artifacts = ingest_module.prepare(
        path, workspace=tmp_path, corpus=corpus)

    second_path = tmp_path / "component-two.json"
    second_data = dict(first_data, product="component-two", version="2",
                       source_id="component_two")
    second_path.write_text(json.dumps(second_data))
    second_controller, _, second_meta, second_artifacts = ingest_module.prepare(
        second_path, workspace=tmp_path, corpus=corpus)

    assert first_controller._state["run_id"] != second_controller._state["run_id"]
    assert first_controller._state["parent_run_id"] == "shared-acquisition"
    assert second_controller._state["parent_run_id"] == "shared-acquisition"
    assert first_meta["parent_run_id"] == second_meta["parent_run_id"] == "shared-acquisition"
    assert first_artifacts[0]["sha256"] == second_artifacts[0]["sha256"]
    assert first_controller.plan["agent"]["version"] == "1"
    assert second_controller.plan["component_two"]["version"] == "2"


def test_cross_run_alias_uses_durable_hash_claim_as_pending_without_post(
        ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path)
    data = json.loads(path.read_text())
    data["run_id"] = "first-run"
    data["files"][0]["canonical_url"] = "https://github.example/blob/guide.md"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    requests = []

    def transport(request):
        requests.append(request)
        if request.method == "POST":
            return UpstreamResponse(202, b'{"operation_ids":["op-1"]}')
        return UpstreamResponse(200, b'{"items":[],"total":0,"limit":100,"offset":0}')

    _, first, meta, artifacts = ingest_module.prepare(path, workspace=tmp_path,
                                                       corpus=corpus, transport=transport)
    assert first.submit(artifacts[0]["id"])["status"] == "accepted"
    data["run_id"] = "new-run"
    data["files"][0]["canonical_url"] = "https://raw.githubusercontent.com/example/guide.md"
    path.write_text(json.dumps(data))
    _, second, new_meta, new_artifacts = ingest_module.prepare(path, workspace=tmp_path,
                                                                corpus=corpus, transport=transport)
    ledger = ingest_module._ledger(None, second)
    result = ingest_module.bank_gate(second, new_artifacts[0]["id"], transport, new_meta, ledger)
    assert result.disposition.value == "ingestion-pending"
    assert ingest_module.submit_with_gate(second, new_artifacts[0]["id"], transport,
                                          new_meta, ledger)["status"] == "ingestion-pending"
    assert len([request for request in requests if request.method == "POST"]) == 1


def test_cross_run_resume_attaches_pending_receipt_without_post(
        ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path)
    data = json.loads(path.read_text())
    data["run_id"] = "first-run"
    data["files"][0]["canonical_url"] = "https://github.example/original/guide.md"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    requests = []

    def transport(request):
        requests.append(request)
        if request.method == "POST":
            return UpstreamResponse(202, b'{"operation_ids":["op62d330ff-7d59-4933-a77e-6fe1c3921ef5"]}')
        return UpstreamResponse(200, b'{"status":"completed","operation_id":"op62d330ff-7d59-4933-a77e-6fe1c3921ef5"}')

    _, first, _, artifacts = ingest_module.prepare(
        path, workspace=tmp_path, corpus=corpus, transport=transport)
    accepted = first.submit(artifacts[0]["id"])
    assert accepted["operation_id"] == "op62d330ff-7d59-4933-a77e-6fe1c3921ef5"

    data["run_id"] = "crossrun-attach-verified"
    data["files"][0]["canonical_url"] = "https://raw.githubusercontent.com/example/guide.md"
    path.write_text(json.dumps(data))
    _, second, _, new_artifacts = ingest_module.prepare(
        path, workspace=tmp_path, corpus=corpus, transport=transport)
    result = second.resume(max_submissions=0, max_status_checks=1)

    assert result["submitted"] == 0
    assert result["checked"] == 1
    receipt = result["receipts"][new_artifacts[0]["id"]]
    assert receipt["operation_id"] == accepted["operation_id"]
    assert receipt["status"] == "completed"
    assert receipt["run_id"] == "crossrun-attach-verified"
    assert receipt["context"]["provenance"][0]["source_url"] == \
        "https://github.example/original/guide.md"
    assert len([request for request in requests if request.method == "POST"]) == 1


def test_foreign_bank_and_corpus_are_rejected(ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    data = json.loads(path.read_text())
    data["bank"] = "Other"
    path.write_text(json.dumps(data))
    with pytest.raises(BoundaryError, match="bank is fixed"):
        ingest_module.prepare(path, workspace=tmp_path, corpus=corpus)
    data["bank"] = "Liv"
    data["corpus_root"] = str(tmp_path / "foreign")
    path.write_text(json.dumps(data))
    with pytest.raises(BoundaryError, match="DEFAULT_CORPUS"):
        ingest_module.prepare(path, workspace=tmp_path)


def test_gate_uses_real_prepared_artifact_hash_and_inventory_once(ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    calls = []

    def transport(request):
        calls.append(request)
        return UpstreamResponse(200, b'{"items":[],"total":0,"limit":100,"offset":0}')

    _, ingestion, meta, artifacts = ingest_module.prepare(path, workspace=tmp_path, corpus=corpus)
    result = ingest_module.bank_gate(ingestion, artifacts[0]["id"], transport, meta, {})
    assert result.disposition.value == "missing"
    assert artifacts[0]["sha256"] != "0" * 64
    assert calls and all(request.method == "GET" for request in calls)
    before = len(calls)
    ingest_module.bank_gate(ingestion, artifacts[0]["id"], transport, meta, {})
    assert len(calls) == before


def test_multi_url_provenance_uses_a_stable_canonical_url(ingest_module, tmp_path, monkeypatch):
    """Identical bytes at several URLs still ingest, with aliases preserved."""
    path, corpus = manifest(tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)

    def transport(request):
        return UpstreamResponse(200, b'{"items":[],"total":0,"limit":100,"offset":0}')

    _, ingestion, meta, artifacts = ingest_module.prepare(path, workspace=tmp_path, corpus=corpus)
    artifact_id = artifacts[0]["id"]
    entry = ingestion.state["artifacts"][artifact_id]
    first = dict(entry["provenance"][0])
    entry["provenance"] = [dict(first, source_url="https://example.test/z-copy"),
                           dict(first, source_url="https://example.test/a-copy")]

    result = ingest_module.bank_gate(ingestion, artifact_id, transport, meta, {})
    assert result.disposition.value == "missing"
    # Deterministic canonical choice: the lexicographically first URL wins, and
    # every URL stays in the provenance the retention context records.
    assert ingest_module.canonical_source_url(entry["provenance"]) == "https://example.test/a-copy"
    assert [row["source_url"] for row in entry["provenance"]] == [
        "https://example.test/z-copy", "https://example.test/a-copy"]


def test_provenance_without_any_source_url_stays_unknown(ingest_module, tmp_path, monkeypatch):
    """A gate result must serialize its disposition; the supervisor reads __dict__."""
    path, corpus = manifest(tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)

    def transport(request):
        return UpstreamResponse(200, b'{"items":[],"total":0,"limit":100,"offset":0}')

    _, ingestion, meta, artifacts = ingest_module.prepare(path, workspace=tmp_path, corpus=corpus)
    artifact_id = artifacts[0]["id"]
    entry = ingestion.state["artifacts"][artifact_id]
    entry["provenance"] = [{"id": "x", "path": "docs/guide.md", "sha256": entry["sha256"]}]

    result = ingest_module.bank_gate(ingestion, artifact_id, transport, meta, {})
    payload = json.loads(json.dumps(result.__dict__))
    assert payload["disposition"] == "unknown"
    assert "canonical source URL" in payload["reason"]


def test_bank_gate_reuses_one_inventory_snapshot_for_all_artifacts(ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path)
    second = corpus / "docs" / "reference.md"
    second.write_text("# reference\n", encoding="utf-8")
    data = json.loads(path.read_text())
    data["files"].append({
        "path": "docs/reference.md",
        "canonical_url": "https://example.test/reference.md",
        "sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
    })
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    calls = []

    def transport(request):
        calls.append(request)
        return UpstreamResponse(200, b'{"items":[],"total":0,"limit":100,"offset":0}')

    _, ingestion, meta, artifacts = ingest_module.prepare(path, workspace=tmp_path, corpus=corpus)
    assert len(artifacts) == 2
    for artifact in artifacts:
        result = ingest_module.bank_gate(ingestion, artifact["id"], transport, meta, {})
        assert result.disposition.value == "missing"
    assert len(calls) == 1
    assert meta["_inventory_checkpoint_state"]["result"].documents == ()


def test_caller_is_default_and_manifest_caller_cannot_override(ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    _, ingestion, meta, _ = ingest_module.prepare(path, workspace=tmp_path, corpus=corpus)
    assert ingestion.caller == "default"
    assert meta["caller"] == "default"
    data = json.loads(path.read_text())
    data["caller"] = "Liv"
    path.write_text(json.dumps(data))
    with pytest.raises(BoundaryError, match="caller is fixed to default"):
        ingest_module.prepare(path, workspace=tmp_path, corpus=corpus)


def test_resume_status_only_does_not_submit(ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    requests = []

    def transport(request):
        requests.append(request)
        if request.method == "POST":
            raise AssertionError("status-only resume submitted")
        return UpstreamResponse(200, b'{"status":"processing","operation_id":"op-1"}')

    _, ingestion, _, artifacts = ingest_module.prepare(path, workspace=tmp_path, corpus=corpus,
                                                       transport=transport)
    # Seed the durable interrupted receipt without issuing a POST.
    with ingestion._ledger_lock():
        ingestion.state["receipts"][artifacts[0]["id"]] = {
            "artifact_id": artifacts[0]["id"], "sha256": artifacts[0]["sha256"],
            "bank": "Liv", "caller": "default", "run_id": ingestion.run_id,
            "operation_id": "op-1", "status": "processing", "polls": 0,
        }
        ingestion._save_receipt(ingestion.state["receipts"][artifacts[0]["id"]])
    result = ingestion.resume(max_submissions=0, max_status_checks=1)
    assert result["submitted"] == 0
    assert not [request for request in requests if request.method == "POST"]


def test_bank_gate_uses_bounded_inventory_defaults(ingest_module, tmp_path, monkeypatch):
    path, corpus = manifest(tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", corpus)
    seen = {}
    original = ingest_module.HindsightInventoryReader

    class Reader(original):
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ingest_module, "HindsightInventoryReader", Reader)
    transport = lambda request: UpstreamResponse(200, b'{"items":[],"total":0,"limit":100,"offset":0}')
    _, ingestion, meta, artifacts = ingest_module.prepare(path, workspace=tmp_path, corpus=corpus)
    result = ingest_module.bank_gate(ingestion, artifacts[0]["id"], transport, meta, {})
    assert result.disposition.value == "missing"
    assert seen["max_pages"] == 2
    assert seen["detail_reads"] == 0


def test_dev_binding_is_fixed_and_copies_sources_without_network(ingest_module, tmp_path, monkeypatch):
    path, source = manifest(tmp_path)
    dev = tmp_path / "dev-hindsight"
    env = tmp_path / "dev.env"
    monkeypatch.setattr(ingest_module, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ingest_module, "DEFAULT_CORPUS", source)
    monkeypatch.setattr(ingest_module, "DEV_CORPUS", dev)
    monkeypatch.setattr(ingest_module, "DEV_ENV", env)
    monkeypatch.setattr(ingest_module, "DEV_LEDGER", dev / "ledger.json")
    _, ingestion, meta, artifacts = ingest_module.prepare(
        path, workspace=tmp_path, dev_instance=True)
    assert meta["bank"] == "Liv"
    assert meta["corpus_root"] == str(dev)
    assert (dev / "docs" / "guide.md").read_bytes() == (source / "docs" / "guide.md").read_bytes()
    assert not (dev / "ledger.json").exists()
    assert artifacts


def test_dev_transport_never_uses_production_credential_or_endpoint(ingest_module, tmp_path, monkeypatch):
    dev_env = tmp_path / "dev.env"
    dev_env.write_text("HINDSIGHT_API_KEY=dev-key\n", encoding="utf-8")
    monkeypatch.setattr(ingest_module, "DEV_ENV", dev_env)
    transport = ingest_module._transport(True, dev_instance=True)
    assert transport.base_url == "http://127.0.0.1:18888"
    assert transport._api_key == "dev-key"
    monkeypatch.setattr(ingest_module, "HERMES_ENV", tmp_path / "production.env")
    assert ingest_module._transport(False, dev_instance=True).__class__ is ingest_module._NoNetwork
    dev_env.unlink()
    with pytest.raises(BoundaryError, match="credential unavailable"):
        ingest_module._transport(True, dev_instance=True)


def test_dev_ledger_rejects_production_path(ingest_module, tmp_path):
    class Ingestion:
        _dev_instance = True
        state = {"receipts": {}}
    with pytest.raises(BoundaryError, match="inside dev corpus"):
        ingest_module._ledger(tmp_path / "production-ledger.json", Ingestion())
