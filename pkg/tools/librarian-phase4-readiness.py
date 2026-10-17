#!/usr/bin/env python3
"""Validate the completed Phase 4 connected rehearsal evidence."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/jsondarula/Liv-Home")
CORPUS = ROOT / "Doc Corpus"
ACQUISITION = CORPUS / "reports/runs/acquire-router-fix-20260908T010000Z"
NATIVE = CORPUS / "reports/runs/phase4-connected-normal-20260919/native-goal-receipt.json"
DEV = CORPUS / "reports/runs/phase4-connected-output-20260919/dev-stage-receipt.json"
INTERRUPTION = ROOT / "backup-notes/dev-interruption-evidence-20260919-final.json"
REQUEST = CORPUS / "rehearsal/ripgrep-normal-rehearsal-phase4-20260908T002500Z/request.json"
OUTPUT = CORPUS / "reports/runs/phase4-readiness-20260919/connected-readiness.json"
PYTHON = Path("/home/jsondarula/.hermes/hermes-agent/venv/bin/python")


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    request = read(REQUEST)
    native = read(NATIVE)
    dev = read(DEV)
    interruption = read(INTERRUPTION)
    manifest = read(ACQUISITION / "manifest.json")
    index = read(ACQUISITION / "component-manifest-index.json")
    gaps = read(ACQUISITION / "gaps.json")
    challenge = read(ACQUISITION / "completeness-challenge.json")

    gate_command = [str(PYTHON), str(ROOT / "tools/librarian-acquisition-verify.py"),
                    "--run-dir", str(ACQUISITION), "--corpus-root", str(CORPUS)]
    gate = subprocess.run(gate_command, cwd=ROOT, text=True, capture_output=True,
                          timeout=180, check=False)
    gate_result = json.loads(gate.stdout) if gate.returncode == 0 else {"stderr": gate.stderr}

    prepared = read(ACQUISITION / "prepare-final.stdout")
    receipt = next(iter(dev.get("helper", {}).get("receipts", {}).values()), {})
    facts_only = set(request) == {"schema_version", "products"}
    hermes = Path("/home/jsondarula/.hermes/hermes-agent/venv/bin/hermes")
    model = subprocess.check_output(
        [str(hermes), "--profile", "librarian", "config", "get", "model.default"],
        cwd=ROOT, text=True, timeout=30).strip()
    provider = subprocess.check_output(
        [str(hermes), "--profile", "librarian", "config", "get", "model.provider"],
        cwd=ROOT, text=True, timeout=30).strip()
    checks = {
        "request_facts_only": facts_only,
        "requested_product": request.get("products") == [{"id": "ripgrep", "version": "14.1.0"}],
        "profile_model": model == "glm-5.3-flash",
        "profile_provider": provider == "litellm",
        "protocol_loaded": "OPERATING_PROTOCOL.md" in (
            CORPUS / "reports/runs/ripgrep-completion-native-20260919b/native-goal.log").read_text(
                encoding="utf-8", errors="replace"),
        "acquisition_gate": gate.returncode == 0 and gate_result.get("ok") is True,
        "component_count": len(index.get("components", [])) == 11,
        "gaps_complete": gaps.get("status") == gaps.get("semantic_status") == "complete" and not gaps.get("gaps"),
        "challenge_passed": challenge.get("status") == "passed",
        "prepared": prepared.get("status") == "prepared" and len(prepared.get("artifacts", [])) > 0,
        "native_goal_terminal": native.get("status") == "completed" and native.get("verifier_passed") is True,
        "native_cleanup": native.get("cleanup_verified") is True,
        "fixture_destination": receipt.get("bank") == "Liv" and receipt.get("caller") == "default",
        "pending_attached": receipt.get("status") == "pending" and bool(receipt.get("operation_id")),
        "zero_duplicate_submit": dev.get("helper", {}).get("submitted") == 0,
        "interrupted_resume": interruption.get("fixture", {}).get("status") == "passed",
        "interrupted_no_repost": interruption.get("fixture", {}).get("posts_before_restart") == 1
            and interruption.get("fixture", {}).get("posts_after_restart") == 1,
    }
    result = {
        "schema": 1, "status": "passed" if all(checks.values()) else "failed",
        "milestone": "ready-for-bounded-live-hindsight-testing",
        "production_ready": False, "live_submission_performed": False,
        "simulated_services": [
            "isolated Hindsight fixture endpoint on 127.0.0.1:18888 with workers disabled and mock LLM",
            "fixture interruption transport for crash-after-acceptance replay protection",
        ],
        "request": str(REQUEST.relative_to(ROOT)),
        "acquisition_run": str(ACQUISITION.relative_to(ROOT)),
        "source_manifest_sha256": sha(ACQUISITION / "manifest.json"),
        "native_session_id": native.get("session_id"),
        "resolved_model": model, "resolved_provider": provider,
        "operation_id": receipt.get("operation_id"),
        "checks": checks,
        "gate_result": gate_result,
        "known_limitations": [
            "The dev operation remains pending because fixture workers are disabled.",
            "This rehearsal proves the declared ripgrep 14.1.0/component boundary, not arbitrary ecosystem completeness.",
            "Normal same-user Hermes execution is not an OS-level adversarial sandbox.",
            "Production rollout still requires an authorized live pilot with terminal ingestion and scoped recall.",
        ],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(OUTPUT),
                      "checks": len(checks)}, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
