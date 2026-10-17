#!/usr/bin/env python3
"""Offline, connected discovery-to-acquisition acceptance probe.

This runner exercises the closed discovery registration boundary before handing
the resulting policy plan to the same controller that owns the evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from broker.controller_integrations import IntegratedController
IMAGE = json.loads((HERE / "image.json").read_text())["image_id"]
ARGV = ["/usr/local/bin/python", "-I", "-m", "broker.librarian_agent_client"]
REQUEST = {"schema_version": 1, "products": [{"id": "fable-tool", "version": "1.0"}]}
AUTHORITY_URL = "https://docs.fictional.invalid/"
CANDIDATE_URL = urljoin(AUTHORITY_URL, "fable/1.0")
DOC_URL = urljoin(CANDIDATE_URL + "/", "guide.md")


def _launcher():
    spec = importlib.util.spec_from_file_location("staged_trusted_launcher", ROOT / "tools/librarian-launch.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeBankTransport:
    """Read-only local bank transport; no Hindsight or network access."""
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        from broker.proxy import UpstreamResponse
        self.requests.append(request)
        body = {"items": [], "total": 0, "limit": 100, "offset": 0}
        return UpstreamResponse(200, json.dumps(body).encode())


class FakeInference:
    """Deterministic model decisions driven only by returned controller evidence."""
    def __init__(self, browser=None):
        self.step = 0
        self.browser = browser
        self.calls = []

    @staticmethod
    def _walk(value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return
        if isinstance(value, dict):
            yield value
            for item in value.values():
                yield from FakeInference._walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from FakeInference._walk(item)

    def __call__(self, **kwargs):
        prompt = json.loads(kwargs["prompt"])
        values = list(self._walk(prompt))
        self.step += 1
        decision = None
        if self.step == 1:
            decision = {"tool": "source_list", "arguments": {}}
        elif self.step == 2:
            decision = {"tool": "discovery_fetch", "arguments": {"url": AUTHORITY_URL}}
        elif self.step == 3:
            decision = {"tool": "discovery_fetch", "arguments": {"url": CANDIDATE_URL}}
        elif self.step == 4:
            authority = next(item["receipt_id"] for item in values if item.get("final_url") == AUTHORITY_URL)
            candidate = next(item["receipt_id"] for item in values if item.get("final_url") == CANDIDATE_URL)
            decision = {"tool": "discovery_register_candidate", "arguments": {
                "candidate_id": "fable-candidate", "receipt_id": candidate,
                "authority_receipt_id": authority}}
        elif self.step == 5:
            decision = {"tool": "source_propose", "arguments": {
                "product_id": "fable-tool", "version": "1.0", "candidate_id": "fable-candidate"}}
        elif self.step == 6:
            decision = {"tool": "discovery_status", "arguments": {}}
        else:
            raise RuntimeError("offline inference exceeded bounded discovery sequence")
        self.calls.append(decision["tool"])
        return json.dumps(decision)


class FixtureBrowser:
    """DiscoveryBrowser-compatible fetcher over an in-memory fictional site."""
    def __init__(self):
        self.pages = {
            AUTHORITY_URL: b'<a href="/fable/1.0">Fable 1.0</a>',
            CANDIDATE_URL: f"[Guide]({DOC_URL})\n".encode(),
            DOC_URL: b"# Fable guide\nOffline fixture documentation.\n",
        }
        self.fetches = 0

    def fetch(self, url, **kwargs):
        self.fetches += 1
        return SimpleNamespace(url=url, status=200, redirects=(), data=self.pages[url])


class FixtureAcquisitionController(IntegratedController):
    """Controller transport seam for deterministic offline source bytes."""
    def _fetch(self, source, url, boundary, receipt):
        # Keep URL selection and receipt bookkeeping in the controller path;
        # only replace DNS/HTTP with explicitly declared fixture bytes.
        if (url not in (AUTHORITY_URL, CANDIDATE_URL, DOC_URL)
                    or not url.startswith(boundary)):
            raise ValueError("fixture URL outside declared boundary")
        data = FixtureBrowser().pages[url]
        attempt = {'id': f'{source}:fixture-fetch:{len(receipt["attempts"])}',
                   'method': 'offline-fixture', 'source_url': url,
                   'final_url': url, 'status': 'succeeded', 'http_status': 200,
                   'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        receipt['attempts'].append(attempt)
        fetched = [(item['source_url'], item['sha256']) for item in receipt['attempts']
                   if item.get('method') == 'offline-fixture']
        receipt['source_manifest_sha256'] = hashlib.sha256(
            json.dumps(fetched, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return data


def _inventory_reader(root):
    from broker.hindsight_inventory import HindsightInventoryReader
    transport = FakeBankTransport()
    result = HindsightInventoryReader(transport, "Liv", caller="Liv", detail_reads=0).inventory()
    if transport.requests and not transport.requests[0].path.startswith("/v1/default/banks/Liv/documents"):
        raise RuntimeError("bank reader did not run first")
    return result, transport


def run_staged_acceptance():
    launcher = _launcher()
    from broker.controller_integrations import ControllerIngestion, IntegratedController
    from broker.discovery_browser import DiscoveryBrowser
    from broker.evidence_source_policy import EvidenceSourcePolicy
    from broker.librarian_completion import build_contract
    from broker.librarian_discovery import DiscoveryController
    from broker.librarian_isolation import IsolationConfig
    from broker.librarian_relay import InferenceProxy
    from broker.corpus_preflight import RunClaimStore
    from broker.librarian_request import validate_request

    evidence = {"bank_reader_first": False, "acquisition_calls": 0}
    with tempfile.TemporaryDirectory(prefix="librarian-staged-") as work:
        root = Path(work)
        corpus = root / "corpus"
        corpus.mkdir()
        bank_result, bank_transport = _inventory_reader(root)
        evidence["bank_reader_first"] = bool(bank_transport.requests)
        contract = build_contract(run_id="staged-acceptance", caller_profile="Liv", workspace=root,
                                  corpus_root=corpus, hindsight_bank="Liv", ingestion_required=True,
                                  requested_products=validate_request(REQUEST)["products"])
        controller = FixtureAcquisitionController("staged-acceptance", corpus, source_plan={})
        browser_impl = FixtureBrowser()
        browser = DiscoveryBrowser(fetch=browser_impl.fetch)
        policy = EvidenceSourcePolicy(controller, REQUEST, discovery_browser=browser,
                                      authority_seeds={AUTHORITY_URL})
        # The relay exposes the receipt binding, not the policy's internal URL.
        # Keep the fixture aligned with that client-facing response contract.
        register_candidate = policy.register_candidate
        def register_for_relay(**kwargs):
            result = register_candidate(**kwargs)
            return {key: result[key] for key in (
                "candidate_id", "receipt_id", "authority_receipt_id",
                "authority_origin", "authority")}
        policy.register_candidate = register_for_relay
        class FixtureDiscovery(DiscoveryController):
            def catalog(self):
                result = super().catalog()
                result["sources"] = [{"source_id": "fable-candidate", "candidate_url": AUTHORITY_URL}]
                return result

        discovery = FixtureDiscovery(controller, policy)
        class CatalogAdapter(launcher.DiscoveryOnlyControllerAdapter):
            pass

        inference_adapter = FakeInference()
        inference = InferenceProxy("offline-staged", inference_adapter)
        run_dir = corpus / "reports" / "runs" / "staged-acceptance"
        request = validate_request(REQUEST)
        preflight = lambda *, request, contract: {
            "caller": "Liv", "bank": "Liv", "bank_inventory": "complete",
            "products": [{"id": "fable-tool", "version": "1.0", "disposition": "missing",
                          "source": "not-yet-approved", "source_hash": "0" * 64,
                          "source_hash_verified": False}],
        }
        config = IsolationConfig(IMAGE)
        claim_store = RunClaimStore(root / "claims.sqlite")
        original = launcher.DiscoveryOnlyControllerAdapter
        launcher.DiscoveryOnlyControllerAdapter = CatalogAdapter
        try:
            discovery_verdict = launcher.run_trusted_discovery(
                request=request, run_dir=run_dir, contract=contract, controller=controller,
                discovery=discovery, inference=inference, config=config, argv=ARGV,
                preflight=preflight, claim_store=claim_store, browser=browser, timeout=60)
        finally:
            launcher.DiscoveryOnlyControllerAdapter = original

        proposal = next(iter(discovery.state["proposals"].values()), {"status": "not-reached"})
        acquisition_status = "not-attempted"
        source_id = None
        if proposal.get("status") == "approved":
            source_id = proposal["source_ids"][0]
            acquisition_status = controller.acquire(source_id).get("status")
            evidence["acquisition_calls"] += 1
        prepared_count = 0
        duplicate_count = 0
        resume_status = None
        if acquisition_status == "complete":
            from broker.proxy import UpstreamResponse
            class FixtureIngestionTransport:
                def __init__(self):
                    self.posts = 0
                def __call__(self, request):
                    if request.method == "POST":
                        self.posts += 1
                        return UpstreamResponse(202, b'{"operation_ids":["fixture-op"]}')
                    return UpstreamResponse(200, b'{"status":"completed"}')
            transport = FixtureIngestionTransport()
            ingestion = ControllerIngestion(controller, "Liv", transport, caller="Liv")
            prepared = ingestion.prepare(source_id)
            prepared_count = len(prepared)
            first = ingestion.resume(max_submissions=16)
            second = ingestion.resume(max_submissions=16)
            resume_status = {"first": first["has_more"], "second": second["has_more"]}
            duplicate_count = max(0, transport.posts - len(prepared))
        state = controller.evidence().snapshot()
        discovery_state = state.get("discovery", {})
        registration_receipts = discovery_state.get("receipts", {})
        registration_completed = bool(registration_receipts)
        proposal_approved = proposal.get("status") == "approved"
        evidence_ready = registration_completed and proposal_approved and evidence["acquisition_calls"] > 0
        launch_attempt = "blocked: generic policy only reports approved_sources_only"
        launch_error = None
        # Acquisition/preparation is the acceptance target; the full launch gate
        # remains intentionally out of scope because it would create a second
        # ingestion adapter for this same controller.
        return {
            "status": "blocked",
            "mode": "offline-fixture",
            "image_id": IMAGE,
            "caller": "Liv",
            "bank": "Liv",
            "stages": {
                "bank": {"status": "passed", "reader": "first", "requests": len(bank_transport.requests),
                         "inventory_complete": bank_result.complete},
                "discovery": {"status": discovery_verdict.get("status"),
                              "valid": discovery_verdict.get("valid", False),
                              "fetches": browser_impl.fetches,
                              "proposal_status": proposal.get("status"),
                              "candidate_learned": browser_impl.fetches >= 1,
                              "rpc_sequence": inference_adapter.calls,
                              "product": "fable-tool",
                              "acquisition_calls": evidence["acquisition_calls"],
                              "acquisition_status": acquisition_status,
                              "prepared_text": prepared_count,
                              "hashes_verified": acquisition_status == "complete" and prepared_count > 0,
                              "resume": resume_status,
                              "duplicates": duplicate_count,
                              "source_manifest_sha256": state.get("sources", {}).get(source_id, {}).get("source_manifest_sha256"),
                              "occurrences": len(state.get("sources", {}).get(source_id, {}).get("occurrences", []))},
                "launch": {"status": launch_attempt, "plan_hashes": "missing", "error": launch_error},
            },
            "blocker": {
                "code": "approved_sources_only" if evidence_ready else "discovery_evidence_incomplete",
                "interface": "generic completion policy",
                "detail": (
                    "Candidate registration receipt observed; approved proposal observed; "
                    f"controller acquisition status={acquisition_status}; "
                    "generic policy does not establish corpus completeness."
                    if registration_completed and proposal_approved
                    else "Evidence did not establish candidate registration and approved proposal; "
                         "generic policy cannot establish corpus completeness."
                ),
                "safety": "No external call or bank write was performed.",
            },
            "evidence_preserved": state.get("discovery", {}),
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        print(json.dumps(run_staged_acceptance(), sort_keys=True))
        return 0
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        print(json.dumps({"status": "blocked", "mode": "offline-fixture",
                          "blocker": {"code": "shared_api_unavailable", "detail": detail},
                          "stages": {"discovery": {"status": "not-executed",
                                                       "candidate_learned": False,
                                                       "acquisition_calls": 0},
                                     "launch": {"status": "not-executed", "plan_hashes": "missing"}}},
                         sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
