#!/usr/bin/env python3
"""Connected Librarian acceptance runner (offline by default)."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
IMAGE = json.loads((HERE / "image.json").read_text(encoding="utf-8"))["image_id"]
ARGV = ["/usr/local/bin/python", "-I", "-m", "broker.librarian_agent_client"]
REQUEST = {"schema_version": 1, "products": [{"id": "fixture-docs", "version": "test-scope"}]}
# This is test software metadata, not a task hint or a production source.
FIXTURE_CANDIDATE_URL = "https://docs.example.invalid/fixture"


def _launcher():
    spec = importlib.util.spec_from_file_location("trusted_launcher", ROOT / "tools/librarian-launch.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _fixture_repo(parent: Path) -> tuple[Path, str]:
    """Create a read-only test-scope repository; this is not full-corpus proof."""
    repo = parent / "source-fixture"
    repo.mkdir()
    _git(repo, "init", "-q")
    files = {
        "Documentation/index.md": b"Fixture documentation\n",
        "Documentation/schema.json": b'{"scope":"test"}\n',

        "README.md": b"outside approved documentation root\n",
    }
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Acceptance Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _fixture_inventory_hash(repo: Path, commit: str) -> str:
    """Hash the pinned documentation tree inventory, including paths and blobs."""
    inventory = _git(repo, "ls-tree", "-r", "--full-tree", commit, "--", "Documentation")
    return hashlib.sha256(inventory.encode("utf-8")).hexdigest()


class FakeTransport:
    """In-memory Hindsight-shaped transport; it never contacts production."""
    def __init__(self):
        self.requests = []
        self.polls = 0

    def __call__(self, request):
        from broker.proxy import UpstreamResponse
        self.requests.append(request)
        if request.method == "POST":
            body = json.dumps({"operation_ids": ["offline-op-" + str(len(self.requests))]}).encode()
            return UpstreamResponse(202, body)
        self.polls += 1
        status = "pending" if self.polls == 1 else "completed"
        return UpstreamResponse(200, json.dumps({"status": status,
                                                  "operation_id": request.path.rsplit("/", 1)[-1]}).encode())


class ObservedAdapter:
    """Record RPC-facing adapter calls without changing their behavior."""
    def __init__(self, adapter):
        self._adapter = adapter
        self.calls = []

    def browser(self, browser):
        """Record the browser methods used by the adapter owned by launch."""
        observed = self

        class Browser:
            def fetch(self, *args, **kwargs):
                observed.calls.append("discovery_fetch")
                return browser.fetch(*args, **kwargs)

            def read(self, *args, **kwargs):
                observed.calls.append("discovery_read")
                return browser.read(*args, **kwargs)

            def enumerate_links(self, *args, **kwargs):
                observed.calls.append("discovery_links")
                return browser.enumerate_links(*args, **kwargs)

        return Browser()

    def wrap(self, name, target):
        def observed(*args, **kwargs):
            self.calls.append(name)
            return target(*args, **kwargs)
        return observed

    def wrap_artifact(self, target):
        def observed(operation, *args, **kwargs):
            self.calls.append(operation.replace(".", "_"))
            return target(operation, *args, **kwargs)
        return observed


    def __getattr__(self, name):
        target = getattr(self._adapter, name)
        if name in {"discovery_fetch", "discovery_read", "discovery_links"}:
            def observed(*args, **kwargs):
                self.calls.append(name)
                return target(*args, **kwargs)
            return observed
        if name == "artifact":
            def observed_artifact(operation, *args, **kwargs):
                self.calls.append(operation.replace(".", "_"))
                return target(operation, *args, **kwargs)
            return observed_artifact
        return target


def _offline_inference(controller, ingestion):
    """Return bounded client decisions from real controller state, not fake calls."""
    def decide(**kwargs):
        state = controller.evidence().snapshot()
        if not getattr(decide, "discovery_started", False):
            decide.discovery_started = True
            return json.dumps({"tool": "source_list", "arguments": {}})
        if not getattr(decide, "candidate_fetched", False):
            prompt = json.loads(kwargs["prompt"])
            catalog = json.loads(prompt["messages"][-1]["content"])
            candidate_url = next(source["candidate_url"] for source in catalog["sources"]
                                 if source["source_id"] == "docs")
            setattr(decide, "candidate_fetched", True)
            return json.dumps({"tool": "discovery_fetch",
                               "arguments": {"url": candidate_url}})
        if "docs" not in state["sources"]:
            return json.dumps({"tool": "source_acquire", "arguments": {"source_id": "docs"}})
        if not ingestion.state["artifacts"]:
            return json.dumps({"tool": "source_prepare", "arguments": {"source_id": "docs"}})
        for artifact_id in sorted(ingestion.state["artifacts"]):
            receipt = ingestion.state["receipts"].get(artifact_id)
            if receipt is None:
                return json.dumps({"tool": "artifact_submit", "arguments": {"artifact_id": artifact_id}})
            if receipt.get("status") in {"accepted", "pending", "processing"}:
                return json.dumps({"tool": "artifact_poll", "arguments": {"artifact_id": artifact_id}})
        if state["sources"]["docs"]["status"] == "complete":
            if not getattr(decide, "checked", False):
                decide.checked = True
                return json.dumps({"tool": "discovery_status", "arguments": {}})
            return json.dumps({"final": "Offline connected acceptance complete; test-scope fixture only."})
        raise RuntimeError("acceptance inference reached an impossible state")
    return decide


def _live_inference():
    key = os.environ.get("LITELLM_API_KEY")
    if not key:
        raise RuntimeError("inference stage: --live-inference requires host LITELLM_API_KEY")
    from broker.librarian_inference import InferenceAdapter
    return InferenceAdapter(key)


def run_acceptance(*, live_inference: bool = False) -> dict:
    stages = {}
    launcher = _launcher()
    from broker.controller_integrations import ControllerIngestion, IntegratedController
    from broker.discovery_browser import DiscoveryBrowser
    from broker.librarian_completion import build_contract
    from broker.librarian_relay import InferenceProxy, RelaySession
    from broker.corpus_preflight import RunClaimStore
    from broker.librarian_isolation import IsolationConfig
    from broker.librarian_request import validate_request
    from broker.librarian_discovery import DiscoveryController, SourceApproval
    from broker.librarian_relay import ControllerAdapter

    class FixtureDiscoveryController(DiscoveryController):
        def catalog(self):
            catalog = super().catalog()
            for source in catalog["sources"]:
                if source["source_id"] == "docs":
                    source["candidate_url"] = FIXTURE_CANDIDATE_URL
                    source["candidate_kind"] = "documentation"
            return catalog

    class FixtureControllerAdapter(ControllerAdapter):
        def bootstrap(self):
            return None

        def catalog(self):
            catalog = super().catalog()
            for source in catalog["sources"]:
                if source["source_id"] == "docs":
                    source["candidate_url"] = FIXTURE_CANDIDATE_URL
                    source["candidate_kind"] = "documentation"
            return catalog

    evidence_dir = Path(os.environ.get("LIBRARIAN_ACCEPTANCE_OUTPUT", HERE / "acceptance-evidence"))
    if evidence_dir.exists():
        shutil.rmtree(evidence_dir)
    with tempfile.TemporaryDirectory(prefix="librarian-e2e-") as work:
        root = Path(work)
        repo, commit = _fixture_repo(root)
        corpus = root / "corpus"
        plan = {"docs": {"kind": "git", "repository": str(repo), "commit": commit,
                          "documentation_roots": ["Documentation"]}}
        controller = IntegratedController("e2e-offline", corpus, plan)
        policy = lambda proposal, evidence: SourceApproval("docs", plan["docs"], "e2e-fixture-policy")             if proposal == {"product_id": "fixture-docs", "version": "test-scope", "candidate_id": "fixture"} else None
        discovery = FixtureDiscoveryController(controller, policy)
        browser = DiscoveryBrowser(fetch=lambda url, **kw: SimpleNamespace(
            url=url, status=200, redirects=(), data=b"# read-only candidate fixture\n"))
        proposal = discovery.propose("source", {"product_id": "fixture-docs", "version": "test-scope", "candidate_id": "fixture"})
        if proposal.get("status") != "approved":
            raise RuntimeError("discovery stage: trusted fixture proposal was not approved")

        transport = FakeTransport()
        request = validate_request(REQUEST)
        contract = build_contract(run_id="e2e-offline", caller_profile="librarian", workspace=root,
                                  corpus_root=corpus, hindsight_bank="Liv", ingestion_required=True,
                                  requested_products=request["products"])
        ingestion = ControllerIngestion(controller, contract["hindsight_bank"], transport,
                                        caller=contract["caller_profile"])
        stages["wiring"] = {"status": "passed", "controller": type(controller).__name__,
                              "relay": "RelaySession", "browser": type(browser).__name__,
                              "inference": "live" if live_inference else "offline-fixture", "hindsight_post": "disabled" if not live_inference else "not used"}
        run_dir = corpus / "reports" / "runs" / "e2e-offline"
        inference_adapter = _live_inference() if live_inference else _offline_inference(controller, ingestion)
        route = "glm-5.3-flash" if live_inference else "offline-fixture-route"
        inference = InferenceProxy(route, inference_adapter, private_values=((os.environ.get("LITELLM_API_KEY"),) if live_inference else ()))
        original_adapter_class = launcher.CanonicalControllerAdapter
        launcher.CanonicalControllerAdapter = FixtureControllerAdapter
        adapter = FixtureControllerAdapter(
            controller, ingestion, discovery=discovery, browser=browser)
        observed = ObservedAdapter(adapter)
        observed_browser = observed.browser(browser)
        original_artifact = adapter.artifact
        setattr(adapter, "artifact", observed.wrap_artifact(original_artifact))
        session = RelaySession(adapter, inference=inference)
        preflight = lambda *, request, contract: {"caller": contract["caller_profile"], "bank": contract["hindsight_bank"],
            "products": [{"id": p["id"], "version": p["version"], "disposition": "missing",
                          "source": "fixture-read-only", "source_hash": _fixture_inventory_hash(repo, commit),
                          "source_hash_verified": True} for p in request["products"]]}
        try:
            verdict = launcher.run_trusted_launch(request=request, run_dir=run_dir, contract=contract,
                controller=controller, discovery=discovery, ingestion=ingestion, inference=inference,
                config=IsolationConfig(IMAGE), argv=ARGV, preflight=preflight,
                claim_store=RunClaimStore(root / "claims.sqlite"), browser=observed_browser, timeout=60)
        except Exception as exc:
            raise RuntimeError("relay stage: " + type(exc).__name__) from exc
        finally:
            launcher.CanonicalControllerAdapter = original_adapter_class
        if not verdict.get("valid"):
            failure = {"status": "failed", "stage": "trusted_launch", "verdict": verdict}
            diagnostic_reader = getattr(inference_adapter, "diagnostic_receipt", None)
            if callable(diagnostic_reader):
                failure["inference"] = diagnostic_reader()
            failure_dir = HERE / "acceptance-evidence"
            failure_dir.mkdir(exist_ok=True)
            (failure_dir / "failure.json").write_text(json.dumps(failure, sort_keys=True) + "\n")
            raise RuntimeError("ingestion stage: " + "; ".join(verdict.get("errors", [])))
        posts_before_restart = len([r for r in transport.requests if r.method == "POST"])
        restarted_controller = IntegratedController("e2e-offline", corpus, plan)
        resumed = ControllerIngestion(restarted_controller, contract["hindsight_bank"], transport,
                                      caller=contract["caller_profile"])
        for artifact in resumed.prepare("docs"):
            resumed.submit(artifact["id"])
        posts_after_restart = len([r for r in transport.requests if r.method == "POST"])
        if posts_after_restart != posts_before_restart:
            raise RuntimeError("resume stage: duplicate submission detected after ledger restart")
        # Crash after a verified 202, before the first status poll; a fresh owner
        # must recover the pending operation without replaying the POST.
        interrupted_transport = FakeTransport()
        interrupted_corpus = root / "interrupted-corpus"
        interrupted = IntegratedController("e2e-interrupted", interrupted_corpus, plan)
        interrupted_ingestion = ControllerIngestion(
            interrupted, contract["hindsight_bank"], interrupted_transport,
            caller=contract["caller_profile"])
        interrupted.acquire("docs")
        interrupted_artifact = interrupted_ingestion.prepare("docs")[0]["id"]
        accepted = interrupted_ingestion.submit(interrupted_artifact)
        if accepted.get("status") != "accepted":
            raise RuntimeError("resume stage: fixture POST was not accepted")
        recovered_controller = IntegratedController("e2e-interrupted", interrupted_corpus, plan)
        recovered = ControllerIngestion(
            recovered_controller, contract["hindsight_bank"], interrupted_transport,
            caller=contract["caller_profile"])
        resumed_result = recovered.resume(max_submissions=0, max_status_checks=1)
        interrupted_posts = len([r for r in interrupted_transport.requests if r.method == "POST"])
        if interrupted_posts != 1 or resumed_result["checked"] != 1:
            raise RuntimeError("resume stage: pending operation was not polled safely")
        fetch_count = observed.calls.count("discovery_fetch")
        read_count = observed.calls.count("discovery_read")
        links_count = observed.calls.count("discovery_links")
        if fetch_count < 1 and not live_inference:
            raise RuntimeError("discovery stage: offline discovery fixture was not exercised")
        # The source is deliberately preapproved. Fetching its advertised
        # candidate proves candidate evidence only, not unseen-product discovery.
        stages["discovery"] = {
            "status": "not_claimed",
            "candidate_evidence": "fetched" if fetch_count else "not_exercised",
            "rpc_calls": sorted(set(observed.calls) & {
                "discovery_fetch", "discovery_read", "discovery_links"}),
            "fetches": fetch_count,
            "reads": read_count,
            "links": links_count,
            "read_required": False,
            "scope": "preapproved fixture source; independent unseen-product discovery is separate coverage",
        }
        stages["relay"] = {"status": "passed", "container_image": IMAGE, "docker": "actual immutable image"}
        stages["ingestion"] = {"status": "passed", "prepared": len(ingestion.state["artifacts"]),
                                "posts_before_restart": posts_before_restart,
                                "posts_after_restart": posts_after_restart,
                                "duplicate_submissions": posts_after_restart - posts_before_restart,
                                "ledger_restart": "passed",
                                "caller": contract["caller_profile"],
                                "pending_recovery": "passed",
                                "pending_posts": interrupted_posts,
                                "pending_polls": resumed_result["checked"]}
        stages["evidence"] = {"status": "passed", "requested_output": str(evidence_dir),
                               "result_json": str(evidence_dir / "result.json")}
        (run_dir / "acceptance-evidence.json").write_text(json.dumps({
            "status": "passed", "requested_output": str(evidence_dir),
            "rpc_calls": sorted(set(observed.calls)),
            "duplicate_submissions": 0,
        }, sort_keys=True) + "\n", encoding="utf-8")
        shutil.copytree(run_dir, evidence_dir)
        return {"status": "passed", "mode": "live-inference" if live_inference else "offline-fixture",
                "scope": "read-only source fixture; test scope, not complete corpus",
                "image_id": IMAGE, "stages": stages}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-inference", action="store_true", help="use host LITELLM_API_KEY; never sent to Docker")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run_acceptance(live_inference=args.live_inference), sort_keys=True))
        return 0
    except Exception as exc:
        message = str(exc).split(":", 1)[0] + ": unavailable"
        print(json.dumps({"status": "failed", "error": message}), file=sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
