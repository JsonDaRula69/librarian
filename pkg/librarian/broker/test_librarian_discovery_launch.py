import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from broker.corpus_preflight import RunClaimStore
from broker.librarian_completion import build_contract
from broker.librarian_discovery import DiscoveryController
from broker.librarian_request import validate_request


LAUNCHER = Path(__file__).resolve().parents[1] / "tools" / "librarian-launch.py"
spec = importlib.util.spec_from_file_location("librarian_launcher_discovery", LAUNCHER)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


REQUEST = {"schema_version": 1, "products": [{"id": "example/app", "version": "1"}]}


class Evidence:
    def __init__(self, run_id, root):
        self.run_id, self.root = run_id, root

    def snapshot(self):
        return {"run_id": self.run_id, "corpus_root": str(self.root), "sources": {}}


class Controller:
    def __init__(self, root, run_id):
        self.root = root
        self.plan = {}
        self._state = {"run_id": run_id, "corpus_root": str(root), "plan": {}, "sources": {}}
        self._evidence = Evidence(run_id, root)

    def evidence(self):
        return self._evidence


class Browser:
    def fetch(self, url):
        return {"authority": "unverified_candidate", "receipt_id": "a" * 32,
                "requested_url": url, "final_url": url, "http_status": 200,
                "sha256": "b" * 64, "redirects": [], "text_length": 5, "truncated": False}

    def read(self, receipt_id, offset, limit):
        return {"receipt_id": receipt_id, "text": "hello", "next_offset": None, "truncated": False}

    def enumerate_links(self, receipt_id):
        return []


class ClaimFailureStore(RunClaimStore):
    def claim(self, *args, **kwargs):
        if args[1] == "example/app":
            raise RuntimeError("claim failure")
        return super().claim(*args, **kwargs)


def setup(tmp_path, monkeypatch, bank_inventory="unknown"):
    root = tmp_path / "corpus"
    root.mkdir()
    run_id = "run-1"
    controller = Controller(root, run_id)
    discovery = DiscoveryController(controller, lambda proposal, evidence: None)
    contract = build_contract(run_id=run_id, caller_profile="caller", workspace=tmp_path,
                              corpus_root=root, hindsight_bank="bank", ingestion_required=False,
                              requested_products=REQUEST["products"])
    inventory = {"caller": "caller", "bank": "bank", "bank_inventory": bank_inventory,
                 "products": [{"id": "example/app", "version": "1", "disposition": "unknown"}]}
    monkeypatch.setattr(launcher, "run_relay", lambda config, argv, session, **kwargs:
                        SimpleNamespace(returncode=0))
    return root, controller, discovery, contract, inventory


def test_unknown_software_without_hash_can_fetch_and_never_ingests(tmp_path, monkeypatch):
    root, controller, discovery, contract, inventory = setup(tmp_path, monkeypatch)
    calls = []

    def relay(config, argv, session, **kwargs):
        response = session.feed((json.dumps({"id": 1, "op": "discovery.fetch",
            "params": {"url": "https://example.test/docs"}}) + "\n").encode())
        calls.append(json.loads(response))
        denied = session.feed((json.dumps({"id": 2, "op": "source.acquire",
            "params": {"source_id": "docs"}}) + "\n").encode())
        calls.append(json.loads(denied))
        return SimpleNamespace(returncode=0, stderr=b'')

    monkeypatch.setattr(launcher, "run_relay", relay)
    verdict = launcher.run_trusted_discovery(
        request=REQUEST, run_dir=root / "reports" / "runs" / "run-1", contract=contract,
        controller=controller, discovery=discovery, inference=None, config=None, argv=[],
        preflight=lambda **kwargs: inventory, claim_store=RunClaimStore(tmp_path / "claims.sqlite"),
        browser=Browser(), timeout=1,
    )
    assert calls[0]["ok"] is True
    assert calls[1] == {"id": 2, "ok": False, "error": "request_rejected"}
    assert verdict["status"] == "reconciliation_required"
    assert verdict["reconciliation_required"] is True
    assert controller._state.get("sources") == {}
    assert store_count(tmp_path / "claims.sqlite") == 1


def store_count(path):
    store = RunClaimStore(path)
    return store._connect().execute("SELECT COUNT(*) FROM run_claims").fetchone()[0]


def test_discovery_binds_caller_and_bank_before_claim(tmp_path, monkeypatch):
    root, controller, discovery, contract, inventory = setup(tmp_path, monkeypatch)
    inventory["bank"] = "other-bank"
    store = RunClaimStore(tmp_path / "claims.sqlite")
    with pytest.raises(launcher.BoundaryError, match="caller/bank"):
        launcher.run_trusted_discovery(
            request=REQUEST, run_dir=root / "reports" / "runs" / "run-1", contract=contract,
            controller=controller, discovery=discovery, inference=None, config=None, argv=[],
            preflight=lambda **kwargs: inventory, claim_store=store, browser=Browser(), timeout=1,
        )
    assert store._connect().execute("SELECT COUNT(*) FROM run_claims").fetchone()[0] == 0


def test_discovery_claim_is_product_version_scoped_and_duplicate_is_denied(tmp_path, monkeypatch):
    root, controller, discovery, contract, inventory = setup(tmp_path, monkeypatch, "complete")
    store = RunClaimStore(tmp_path / "claims.sqlite")
    first = launcher.run_trusted_discovery(
        request=REQUEST, run_dir=root / "reports" / "runs" / "one", contract=contract,
        controller=controller, discovery=discovery, inference=None, config=None, argv=[],
        preflight=lambda **kwargs: inventory, claim_store=store, browser=Browser(), timeout=1,
    )
    assert first["valid"] is False
    assert first["status"] == "partial"
    # The first clean run fences and releases its claim; an active claim is a duplicate.
    fingerprint = launcher.scope_fingerprint("caller", "bank")
    store.claim("caller:bank:discovery", "example/app", "1", fingerprint,
                "other-owner", "other-run", time.time(), 100)
    second = launcher.run_trusted_discovery(
        request=REQUEST, run_dir=root / "reports" / "runs" / "two", contract=contract,
        controller=controller, discovery=discovery, inference=None, config=None, argv=[],
        preflight=lambda **kwargs: inventory, claim_store=store, browser=Browser(), timeout=1,
    )
    assert second["status"] == "attach-existing"


def test_discovery_allowlist_exposes_read_only_client_aliases(tmp_path, monkeypatch):
    root, controller, discovery, contract, inventory = setup(tmp_path, monkeypatch, "complete")
    calls = []

    def relay(config, argv, session, **kwargs):
        for identifier, operation in ((1, "source.list"), (2, "discovery.status")):
            response = session.feed((json.dumps({"id": identifier, "op": operation,
                "params": {}}) + "\n").encode())
            calls.append(json.loads(response))
        return SimpleNamespace(returncode=0, stderr=b'')

    monkeypatch.setattr(launcher, "run_relay", relay)
    result = launcher.run_trusted_discovery(
        request=REQUEST, run_dir=root / "reports" / "runs" / "aliases", contract=contract,
        controller=controller, discovery=discovery, inference=None, config=None, argv=[],
        preflight=lambda **kwargs: inventory, claim_store=RunClaimStore(tmp_path / "claims.sqlite"),
        browser=Browser(), timeout=1,
    )
    assert [call["ok"] for call in calls] == [True, True]
    assert result["status"] == "partial"


def test_discovery_argv_only_injects_flag_for_audited_client():
    assert launcher._discovery_argv(['python', '-m', 'broker.librarian_agent_client'])[-1] == '--discovery-only'
    arbitrary = ['python', '-m', 'other.client']
    assert launcher._discovery_argv(arbitrary) == arbitrary


def test_discovery_rejects_symlink_run_dir_before_mkdir(tmp_path, monkeypatch):
    root, controller, discovery, contract, inventory = setup(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "reports"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(launcher.BoundaryError, match="symlink"):
        launcher.run_trusted_discovery(
            request=REQUEST, run_dir=link / "runs" / "run-1", contract=contract,
            controller=controller, discovery=discovery, inference=None, config=None, argv=[],
            preflight=lambda **kwargs: inventory, claim_store=RunClaimStore(tmp_path / "claims.sqlite"),
            browser=Browser(), timeout=1,
        )
    assert not (outside / "runs").exists()


def test_discovery_releases_own_claims_when_claim_raises(tmp_path, monkeypatch):
    root, controller, discovery, contract, inventory = setup(tmp_path, monkeypatch, "complete")
    store = ClaimFailureStore(tmp_path / "claims.sqlite")
    with pytest.raises(RuntimeError, match="claim failure"):
        launcher.run_trusted_discovery(
            request=REQUEST, run_dir=root / "reports" / "runs" / "run-1", contract=contract,
            controller=controller, discovery=discovery, inference=None, config=None, argv=[],
            preflight=lambda **kwargs: inventory, claim_store=store, browser=Browser(), timeout=1,
        )
    assert store_count(tmp_path / "claims.sqlite") == 0
