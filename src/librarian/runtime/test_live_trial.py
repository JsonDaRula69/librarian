"""Focused safety and incomplete-inventory tests for the live trial."""
import importlib.util
import json
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("live_trial", HERE / "live_trial.py")
assert SPEC is not None and SPEC.loader is not None
live_trial = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live_trial)


def test_incomplete_inventory_marks_requested_products_unknown(monkeypatch):
    class Item:
        product = "docs"
        version = "1"
        verified = False

    class Result:
        documents = (Item(),)
        complete = False
        has_more = True

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        def inventory(self):
            return Result()

    monkeypatch.setitem(__import__("sys").modules, "broker.hindsight_inventory",
                        type("M", (), {"HindsightInventoryReader": Reader}))
    monkeypatch.setitem(__import__("sys").modules, "broker.transport",
                        type("M", (), {"HindsightHTTPTransport": lambda *a, **k: object()}))
    result = live_trial._inventory({"products": [{"id": "docs", "version": "1"}]}, "key")
    assert result["bank_inventory"] == "unknown"
    assert result["products"][0]["disposition"] == "unknown"


def test_verdict_errors_are_persisted_as_categories_only():
    errors = live_trial._safe_verdict_errors([
        "discovery produced no fetched receipt or proposal evidence",
        {"category": "provider_failure", "detail": "secret must not persist"},
    ])
    assert errors == [
        {"category": "discovery_produced_no_fetched_receipt_or_proposal_evidence"},
        {"category": "provider_failure"},
    ]


def test_authority_map_is_validated_and_malformed_map_rejected(tmp_path):
    config = tmp_path / "authority.json"
    config.write_text(json.dumps({"https://help.ubuntu.com": ["https://launchpad.net"]}))
    assert live_trial._load_authority_map(config) == {
        "https://help.ubuntu.com": ["https://launchpad.net"]
    }

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"https://help.ubuntu.com": "https://launchpad.net"}')
    with pytest.raises(ValueError, match="origin=>array"):
        live_trial._load_authority_map(malformed)


def test_programmatic_run_id_rejects_unsafe_path_without_creating_directory():
    with pytest.raises(ValueError):
        live_trial._validate_run_dir("../escape")


def test_artifact_rejects_destination_outside_corpus_without_creating_directory(tmp_path):
    target = tmp_path / "outside"
    with pytest.raises(ValueError):
        live_trial._artifact(target, status="blocked", stages={})
    assert not target.exists()


def test_artifact_writes_into_existing_run_directory(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    run_dir = corpus / "reports" / "runs" / "existing"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(live_trial, "CORPUS", corpus)

    payload = live_trial._artifact(run_dir, status="blocked", stages={})

    assert payload["status"] == "blocked"
    assert (run_dir / "live-trial.json").is_file()


def test_artifact_rejects_symlinked_run_directory(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    run_dir = corpus / "reports" / "runs" / "linked"
    run_dir.parent.mkdir(parents=True)
    run_dir.symlink_to(real_dir, target_is_directory=True)
    monkeypatch.setattr(live_trial, "CORPUS", corpus)

    with pytest.raises(ValueError, match="symlink"):
        live_trial._artifact(run_dir, status="blocked", stages={})
    assert not (real_dir / "live-trial.json").exists()


def test_validate_run_dir_allows_existing_directory(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    run_dir = corpus / "reports" / "runs" / "existing"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(live_trial, "CORPUS", corpus)

    assert live_trial._validate_run_dir("existing") == run_dir


def test_validate_run_dir_rejects_symlinked_directory(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    run_dir = corpus / "reports" / "runs" / "linked"
    run_dir.parent.mkdir(parents=True)
    run_dir.symlink_to(real_dir, target_is_directory=True)
    monkeypatch.setattr(live_trial, "CORPUS", corpus)

    with pytest.raises(ValueError, match="symlink"):
        live_trial._validate_run_dir("linked")


def test_recovery_refuses_unknown_owner_activity(tmp_path):
    from broker.corpus_preflight import RunClaimStore, scope_fingerprint
    db = tmp_path / "claims.sqlite"
    store = RunClaimStore(db)
    scope = scope_fingerprint("default", "Liv")
    store.claim("default:Liv:discovery", "docs", "1", scope, "owner", "old-run",
                time.time() - 10, 1)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    recovered = live_trial._RecoveryClaimStore(store, run_dir, "old-run")
    with pytest.raises(RuntimeError, match="activity is unknown"):
        recovered.claim("default:Liv:discovery", "docs", "1", scope, "new-owner",
                        "old-run", time.time(), 30)


def test_recovery_records_receipt_and_returns_owned_claim_once(tmp_path, monkeypatch):
    from broker.corpus_preflight import RunClaimStore, scope_fingerprint
    db = tmp_path / "claims.sqlite"
    store = RunClaimStore(db)
    scope = scope_fingerprint("default", "Liv")
    now = time.time()
    store.claim("default:Liv:discovery", "docs", "1", scope, "owner", "old-run", now - 10, 1)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "controller-process.json").write_text(json.dumps({"pid": 99999999, "proc_starttime": "1"}))
    recovered = live_trial._RecoveryClaimStore(store, run_dir, "old-run")
    first = recovered.claim("default:Liv:discovery", "docs", "1", scope, "new-owner", "old-run", now, 30)
    second = recovered.claim("default:Liv:discovery", "docs", "1", scope, "new-owner", "old-run", now, 30)
    assert first.status == "claimed"
    assert second.status == "attach-existing"
    receipt = json.loads((run_dir / "recovery-receipt.json").read_text())
    assert receipt["action"] == "recover_expired"
    assert receipt["auto_delete"] is False
