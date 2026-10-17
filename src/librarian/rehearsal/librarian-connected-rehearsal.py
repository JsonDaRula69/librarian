#!/usr/bin/env python3
"""Bounded Phase 4 connected rehearsal using existing launcher/helper paths."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path("/home/jsondarula/Liv-Home")
CORPUS = ROOT / "Doc Corpus"
REQUEST = CORPUS / "rehearsal/ripgrep-normal-rehearsal-phase4-20260908T002500Z/request.json"
MANIFEST = ROOT / "backup-notes/dev-readme-acceptance-manifest.json"
ARCHIVE = CORPUS / "reports/runs/acquire-only-real-20260908T002500Z/sources/ripgrep/14.1.0/ripgrep-14.1.0.tar.gz"
LAUNCHER = ROOT / "tools/librarian-launch.py"
PYTHON = Path("/home/jsondarula/.hermes/hermes-agent/venv/bin/python")
HERMES = Path(os.environ.get("HERMES_BIN", "/home/jsondarula/.hermes/hermes-agent/venv/bin/hermes"))
RUN_ID = "connected-rehearsal-ripgrep-phase4"
EVIDENCE = CORPUS / "reports/runs" / RUN_ID


def run(cmd: list[str], timeout: int) -> dict[str, object]:
    started = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout, check=False)
    return {"command": cmd, "exit_code": proc.returncode, "elapsed_seconds": round(time.time() - started, 2),
            "stdout": proc.stdout, "stderr": proc.stderr}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def endpoint() -> dict[str, object]:
    try:
        with urlopen("http://127.0.0.1:18888/health", timeout=3) as response:
            return {"reachable": True, "status": response.status, "body": response.read().decode("utf-8", "replace")[:500]}
    except Exception as exc:
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    request = json.loads(REQUEST.read_text())
    manifest = json.loads(MANIFEST.read_text())
    readme = CORPUS / manifest["files"][0]["path"]
    facts_only = set(request) == {"schema_version", "products"} and request["products"] == [{"id": "ripgrep", "version": "14.1.0"}]
    manifest_hash_ok = readme.is_file() and sha256(readme) == manifest["files"][0]["sha256"]
    archive_ok = ARCHIVE.is_file()
    report: dict[str, object] = {
        "schema": 1, "run_id": RUN_ID, "phase": "4", "test_only": True,
        "request": str(REQUEST), "request_facts_only": facts_only,
        "manifest": str(MANIFEST), "manifest_hash_ok": manifest_hash_ok,
        "archive_reused": str(ARCHIVE), "archive_present": archive_ok,
        "endpoint_before": endpoint(), "actions": [],
        "normal_command": [str(PYTHON), str(LAUNCHER), "--request-file", str(REQUEST),
                           "--caller-profile", "default", "--workspace", str(ROOT),
                           "--hindsight-bank", "Liv", "--corpus-root", str(CORPUS),
                           "--run-id", RUN_ID, "--foreground", "--acquire-only", "--run-budget", "120"],
        "budgets": {"run_budget_seconds": 120, "max_turns": 10, "dev_submit_max_artifacts": 1},
        "model_expected": "glm-5.3-flash",
        "production_write": False, "terminal_recall": False,
    }
    if not (facts_only and manifest_hash_ok and archive_ok):
        report["status"] = "blocked-preflight"
        (EVIDENCE / "connected-rehearsal.json").write_text(json.dumps(report, indent=2) + "\n")
        return 74

    # Use the normal entrypoint; its acquire-only contract forbids Hindsight POST.
    normal = run(report["normal_command"], timeout=135)
    normal.pop("stdout", None)
    normal.pop("stderr", None)
    report["normal"] = normal

    base = [str(PYTHON), str(LAUNCHER), "--caller-profile", "default", "--workspace", str(ROOT),
            "--hindsight-bank", "Liv", "--corpus-root", str(CORPUS), "--run-id", RUN_ID,
            "--run-budget", "30", "--dev-manifest", str(MANIFEST)]
    for action, allow in (("status", False), ("submit", True), ("resume", True)):
        cmd = base + ["--dev-action", action]
        if allow:
            cmd.append("--allow-dev-submit")
        result = run(cmd, timeout=40)
        result["action"] = action
        report["actions"].append(result)

    receipt = EVIDENCE / "dev-stage-receipt.json"
    report["endpoint_after"] = endpoint()
    report["receipt_present"] = receipt.is_file()
    # The normal launch can time out after leaving a valid resumed receipt; keep
    # that distinction explicit instead of presenting it as a fresh acquisition.
    # This driver uses a pre-existing manifest, not output from the new launch.
    # Successful helper actions therefore cannot certify a connected acquisition.
    report["status"] = "partial"
    report["remaining_gap"] = (
        "The helper manifest predates this launch; acquisition-output lineage "
        "and supported acquisition resumption have not been demonstrated."
    )
    (EVIDENCE / "connected-rehearsal.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "evidence": str(EVIDENCE / "connected-rehearsal.json"),
                      "normal_exit": normal["exit_code"],
                      "actions": [(x["action"], x["exit_code"]) for x in report["actions"]]}, sort_keys=True))
    return 0 if report["status"] == "completed" else 74


if __name__ == "__main__":
    raise SystemExit(main())
