#!/usr/bin/env python3
"""Safe live discovery trial: read-only bank preflight, then isolated discovery."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

IMAGE_FILE = HERE / "image.json"
HINDSIGHT_BASE = "http://127.0.0.1:8888"
INFERENCE_HOST = "127.0.0.1"
INFERENCE_PORT = 4000
INFERENCE_MODEL = "glm-5.3-flash"
BANK = "Liv"
CALLER = "default"
WORKSPACE = ROOT
CORPUS = ROOT / "Doc Corpus"
ARGV = ["/usr/local/bin/python", "-I", "-m", "broker.librarian_agent_client", "--discovery-only"]
from broker.corpus_preflight import RunClaimStore


def _claim_row(path, namespace, product, version, scope):
    """Read the exact claim binding without changing or deleting it."""
    import sqlite3
    with sqlite3.connect(str(path)) as db:
        return db.execute(
            "SELECT lease_owner, run_id, expires_at FROM run_claims "
            "WHERE namespace=? AND product=? AND version=? AND scope_fingerprint=?",
            (namespace, product, version, scope)).fetchone()


def _process_identity(run_dir: Path) -> dict:
    return {"pid": os.getpid(), "proc_starttime": _proc_starttime(os.getpid())}


def _proc_starttime(pid: int):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[21]
    except (OSError, IndexError):
        return None


def _identity_status(identity: dict) -> str:
    pid, start = identity.get("pid"), identity.get("proc_starttime")
    if type(pid) is not int or not isinstance(start, str):
        return "unknown"
    current = _proc_starttime(pid)
    if current is None:
        return "dead"
    return "active" if current == start else "dead"


class _RecoveryClaimStore(RunClaimStore):
    """Recover one expired claim only after proving its recorded owner is dead."""
    def __init__(self, store, run_dir, recovery_run_id):
        self.store, self.run_dir, self.recovery_run_id = store, run_dir, recovery_run_id
        self._recovered = set()

    def claim(self, namespace, product, version, scope_fingerprint, lease_owner, run_id, now, lease_seconds, **kwargs):
        row = _claim_row(self.store.path, namespace, product, version, scope_fingerprint)
        key = (namespace, product, version, scope_fingerprint)
        if not self.recovery_run_id or key in self._recovered or not row:
            return self.store.claim(namespace, product, version, scope_fingerprint, lease_owner, run_id, now, lease_seconds)
        recorded_owner, recorded_run, expires_at = row
        if recorded_run != self.recovery_run_id or run_id != self.recovery_run_id:
            return self.store.claim(namespace, product, version, scope_fingerprint, lease_owner, run_id, now, lease_seconds)
        identity_path = self.run_dir / "controller-process.json"
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            identity = {}
        activity = _identity_status(identity)
        if expires_at > now or activity == "active":
            raise RuntimeError("recovery refused: associated claim is active")
        if activity != "dead":
            raise RuntimeError("recovery refused: associated claim activity is unknown")
        result = self.store.claim(namespace, product, version, scope_fingerprint, lease_owner, run_id, now,
                                  lease_seconds, recover_expired=True)
        if result.status != "claimed":
            raise RuntimeError("recovery refused: claim binding changed")
        self._recovered.add(key)
        receipt = {"schema": "librarian-live-trial-recovery-v1", "run_id": run_id,
                   "namespace": namespace, "product": product, "version": version,
                   "scope_fingerprint": scope_fingerprint, "recorded_owner": recorded_owner,
                   "recorded_run_id": recorded_run, "activity": activity,
                   "action": "recover_expired", "auto_delete": False}
        (self.run_dir / "recovery-receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return result

    def __getattr__(self, name):
        return getattr(self.store, name)


def _persist_browser_receipts(controller, browser, run_dir):
    """Persist only controller-bound, non-secret browser text/URL/hash receipts."""
    evidence = controller.evidence().snapshot()
    if evidence.get("run_id") != run_dir.name:
        raise ValueError("receipt run binding mismatch")
    receipts = []
    for receipt in getattr(browser, "_receipts", {}).values():
        receipts.append({"receipt_id": receipt.receipt_id, "requested_url": receipt.requested_url,
                         "final_url": receipt.final_url, "sha256": receipt.sha256,
                         "text": receipt.text})
    from broker.acquisition_controller import safe_path
    target = safe_path(CORPUS.absolute(), str(run_dir.absolute().relative_to(CORPUS.absolute()) /
                                               "discovery-browser-receipts.json"))
    target.write_text(json.dumps({"schema": "librarian-discovery-receipts-v1",
                                  "run_id": evidence["run_id"], "receipts": receipts},
                                 indent=2, sort_keys=True) + "\n")


def _launcher():
    spec = importlib.util.spec_from_file_location("trusted_live_launcher", ROOT / "tools/librarian-launch.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    try:
        return Path.home() / ".hermes"
    except (RuntimeError, OSError):
        return Path(".hermes")


def _secret(name: str) -> str:
    """Read only the named secret from the host's Hermes env file."""
    path = _hermes_home() / ".env"
    if not path.is_file():
        raise RuntimeError("credential source unavailable")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith(name + "="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                return value
    # The plaintext copy has moved into Infisical; resolve it through this profile's
    # read-only identity rather than failing.
    import subprocess
    resolved = subprocess.run([str(_hermes_home() / "bin" / "liv-secret"), name],
                              capture_output=True, text=True, timeout=15)
    if resolved.returncode == 0 and resolved.stdout.strip():
        return resolved.stdout.strip()
    raise RuntimeError("required credential unavailable")


def _safe_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("authority seed must be a public HTTPS URL without query or fragment")
    return value


def _load_authority_map(path: Path | None) -> dict[str, list[str]] | None:
    """Load strict origin relationships; omission preserves policy default deny."""
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("authority map must be a readable JSON object") from exc
    if not isinstance(value, dict) or any(
            not isinstance(source, str) or not isinstance(targets, list)
            or any(not isinstance(target, str) for target in targets)
            for source, targets in value.items()):
        raise ValueError("authority map must be origin=>array-of-origins JSON")
    from broker.evidence_source_policy import EvidenceSourcePolicy
    try:
        EvidenceSourcePolicy._relationships({}, value)
    except (TypeError, ValueError) as exc:
        raise ValueError("authority map must contain HTTPS origins") from exc
    return value


def _image_id() -> str:
    data = json.loads(IMAGE_FILE.read_text(encoding="utf-8"))
    image = data.get("image_id")
    if not isinstance(image, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise RuntimeError("image.json has no valid immutable image ID")
    return image


def _route_check(port: int, timeout: float = 2.0) -> dict:
    try:
        with socket.create_connection((INFERENCE_HOST, port), timeout=timeout):
            return {"status": "reachable", "host": INFERENCE_HOST, "port": port}
    except (OSError, TimeoutError) as exc:
        return {"status": "blocked", "host": INFERENCE_HOST, "port": port,
                "error_type": type(exc).__name__}


def _inventory(request, key: str, *, bounded: bool = True):
    from broker.hindsight_inventory import HindsightInventoryReader
    from broker.transport import HindsightHTTPTransport

    reader = HindsightInventoryReader(
        HindsightHTTPTransport(HINDSIGHT_BASE, key, timeout_seconds=15),
        BANK, caller=CALLER, max_pages=2 if bounded else 100, detail_reads=0,
    )
    result = reader.inventory()
    observed = {(item.product, item.version): item for item in result.documents}
    products = []
    for product in request["products"]:
        item = observed.get((product["id"], product["version"]))
        products.append({
            "id": product["id"], "version": product["version"],
            "disposition": ("verified-present" if item and item.verified else
                           "unknown" if not result.complete or result.has_more else "missing"),
        })
    return {
        "caller": CALLER, "bank": BANK,
        "bank_inventory": "complete" if result.complete and not result.has_more else "unknown",
        "products": products,
        "documents_observed": len(tuple(result.documents)),
    }


def _validate_run_dir(run_id: str) -> Path:
    """Validate the id and destination before creating any artifact directory."""
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is None:
        raise ValueError("run_id must be a simple identifier")
    from broker.acquisition_controller import safe_path
    relative = Path("reports") / "runs" / run_id
    # Validate a file target so the existing run directory is allowed while
    # safe_path still rejects symlinks anywhere in the path.
    safe_path(CORPUS, str(relative / "live-trial.json"))
    for parent in (CORPUS / "reports", CORPUS / "reports" / "runs", CORPUS / relative):
        if parent.exists() and not parent.is_dir():
            raise ValueError("run artifact parent must be a directory")
    return CORPUS / relative


def _artifact(run_dir: Path, *, status: str, stages: dict, errors: list[dict] | None = None) -> dict:
    from broker.acquisition_controller import safe_path
    root = CORPUS.absolute()
    relative = Path(run_dir).absolute().relative_to(root)
    safe_path(root, str(relative / "live-trial.json"))
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "librarian-live-trial-v1", "status": status,
        "stages": stages, "errors": errors or [], "no_ingestion_write": True,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (run_dir / "live-trial.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def _safe_error(stage: str, exc: BaseException) -> dict:
    return {"stage": stage, "error_type": type(exc).__name__}


def _safe_verdict_errors(errors):
    """Keep verdict failure categories while dropping model/provider text."""
    result = []
    for error in errors if isinstance(errors, list) else []:
        if isinstance(error, str):
            result.append({"category": re.sub(r"[^a-z0-9_]+", "_", error.lower()).strip("_")})
        elif isinstance(error, dict):
            result.append({key: error[key] for key in ("category", "error_type")
                           if isinstance(error.get(key), str)})
    return result


def _validate_image_and_routes(launcher, image: str) -> dict:
    from broker import librarian_isolation as isolation
    isolation.preflight(isolation.IsolationConfig(image))
    return {"image_id": image, "image": "validated", "inference_route": _route_check(INFERENCE_PORT),
            "hindsight_route": _route_check(8888)}


def run(args) -> tuple[int, dict]:
    from broker.librarian_request import parse_request
    from broker.librarian_completion import build_contract
    from broker.librarian_discovery import DiscoveryController
    from broker.evidence_source_policy import EvidenceSourcePolicy
    from broker.discovery_browser import DiscoveryBrowser
    from broker.acquisition_controller import AcquisitionController
    from broker.corpus_preflight import RunClaimStore
    from broker.librarian_inference import LiteLLMInferenceAdapter
    from broker.librarian_relay import InferenceProxy
    from broker.librarian_isolation import IsolationConfig

    request = parse_request(args.request_file.read_text(encoding="utf-8"))
    seed = _safe_url(args.authority_seed)
    authority_map = _load_authority_map(args.authority_map)
    image = _image_id()
    run_dir = _validate_run_dir(args.run_id)
    if args.recover_run is not None and args.recover_run != args.run_id:
        raise ValueError("--recover-run must match --run-id")
    stages: dict = {"mode": "discovery-only", "no_post": True, "no_ingestion_write": True}
    errors: list[dict] = []
    try:
        image_stage = _validate_image_and_routes(_launcher(), image)
        stages["image_and_routes"] = image_stage
        if any(route.get("status") == "blocked" for route in image_stage.values() if isinstance(route, dict)):
            errors.append({"stage": "image_and_routes", "error_type": "BlockedRoute"})
            return 74, _artifact(run_dir, status="blocked", stages=stages, errors=errors)
    except Exception as exc:
        errors.append(_safe_error("image_and_routes", exc))
        return 74, _artifact(run_dir, status="blocked", stages=stages, errors=errors)

    try:
        hindsight_key = _secret("HINDSIGHT_API_KEY")
        inventory = _inventory(request, hindsight_key)
        stages["bank_preflight"] = inventory
        if inventory["bank_inventory"] == "unknown":
            errors.append({"stage": "bank_preflight", "error_type": "IncompleteInventory"})
            if args.preflight_only:
                stages["discovery"] = {"status": "not-run", "reason": "preflight-only", "partial": True}
                return 0, _artifact(run_dir, status="partial", stages=stages, errors=errors)
    except Exception as exc:
        errors.append(_safe_error("bank_preflight", exc))
        return 74, _artifact(run_dir, status="blocked", stages=stages, errors=errors)

    if args.preflight_only:
        stages["discovery"] = {"status": "not-run", "reason": "preflight-only"}
        return 0, _artifact(run_dir, status="preflight_passed", stages=stages, errors=errors)

    try:
        launcher = _launcher()
        contract = build_contract(run_id=args.run_id, caller_profile=CALLER, workspace=WORKSPACE,
                                  corpus_root=CORPUS, hindsight_bank=BANK, ingestion_required=False,
                                  requested_products=request["products"])
        controller = AcquisitionController(args.run_id, CORPUS, {})
        run_dir.mkdir(parents=True, exist_ok=True)
        identity_path = run_dir / "controller-process.json"
        if not identity_path.exists():
            identity_path.write_text(json.dumps(_process_identity(run_dir), sort_keys=True) + "\n")
        browser = DiscoveryBrowser(max_bytes=2 * 1024 * 1024, max_text=256 * 1024, timeout=15)
        policy = EvidenceSourcePolicy(controller, request, discovery_browser=browser,
                                      authority_seeds=[seed],
                                      authority_relationships=authority_map)
        discovery = DiscoveryController(controller, policy)
        inference_key = _secret("LITELLM_API_KEY")
        inference = InferenceProxy(INFERENCE_MODEL, LiteLLMInferenceAdapter(inference_key, max_tokens=512),
                                   max_calls=8, max_output_tokens=512, timeout=25,
                                   private_values=(inference_key,))
        config = IsolationConfig(image)
        claim_store = RunClaimStore(CORPUS / "dedupe" / "live-trial-claims.sqlite")
        if args.recover_run is not None:
            claim_store = _RecoveryClaimStore(claim_store, run_dir, args.recover_run)

        class LiveAdapter(launcher.DiscoveryOnlyControllerAdapter):
            def catalog(self):
                catalog = super().catalog()
                catalog["authority_seed"] = seed
                return catalog

        original_adapter = launcher.DiscoveryOnlyControllerAdapter
        launcher.DiscoveryOnlyControllerAdapter = LiveAdapter
        try:
            verdict = launcher.run_trusted_discovery(
                request=request, run_dir=run_dir, contract=contract, controller=controller,
                discovery=discovery, inference=inference, config=config, argv=ARGV,
                preflight=lambda **_: inventory, claim_store=claim_store, browser=browser,
                timeout=min(args.max_seconds, 180))
        finally:
            launcher.DiscoveryOnlyControllerAdapter = original_adapter
        stages["discovery"] = {"status": verdict.get("status", "blocked"),
                                "valid": bool(verdict.get("valid")), "authority_seed": seed,
                                "catalog_exposes_seed": False, "evidence": controller.evidence().snapshot(),
                                "unsupported_acquisition": "blocked",
                                "diagnostics": verdict.get("diagnostics", {})}
        _persist_browser_receipts(controller, browser, run_dir)
        result = _artifact(run_dir, status=verdict.get("status", "blocked"), stages=stages,
                           errors=([{"stage": "discovery", "error_type": "DiscoveryIncomplete",
                                     "verdict_errors": _safe_verdict_errors(verdict.get("errors")),
                                     "diagnostics": verdict.get("diagnostics", {})}]
                                   if not verdict.get("valid") else []))
        return (0 if verdict.get("valid") else 74), result
    except Exception as exc:
        errors.append(_safe_error("discovery", exc))
        return 74, _artifact(run_dir, status="blocked", stages=stages, errors=errors)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--authority-seed", required=True)
    parser.add_argument("--authority-map", type=Path,
                        help="JSON object mapping trusted origins to linked origins")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--max-seconds", type=int, default=120)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--recover-run", help="recover this exact expired run claim only after owner death is proven")
    args = parser.parse_args(argv)
    if not 1 <= args.max_seconds <= 180:
        parser.error("--max-seconds must be between 1 and 180")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.run_id):
        parser.error("--run-id must be a simple identifier")
    try:
        _validate_run_dir(args.run_id)
        code, result = run(args)
    except Exception as exc:
        code, result = 74, {"schema": "librarian-live-trial-v1", "status": "blocked",
                             "stages": {}, "errors": [_safe_error("input", exc)],
                             "no_ingestion_write": True}
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
