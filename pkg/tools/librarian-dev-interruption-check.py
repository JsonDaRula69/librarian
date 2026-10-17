#!/usr/bin/env python3
"""Prove crash-after-accepted-receipt recovery without replaying a POST.

The supervisor invocation is status-only against the existing dev instance. The
forced interruption uses the existing ControllerIngestion helper against a
separate temporary corpus and an instrumented fixture transport.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "backup-notes/dev-readme-acceptance-manifest.json"
PYTHON = "/home/jsondarula/.hermes/hermes-agent/venv/bin/python"


def load_e2e():
    path = ROOT / "tools/librarian-runtime/e2e_acceptance.py"
    spec = importlib.util.spec_from_file_location("existing_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def supervisor_check(run_id: str) -> dict:
    command = [PYTHON, str(ROOT / "tools/librarian-launch.py"),
               "--dev-manifest", str(MANIFEST), "--dev-action", "resume",
               "--caller-profile", "default", "--workspace", str(ROOT),
               "--hindsight-bank", "Liv", "--run-id", run_id,
               "--run-budget", "30"]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True,
                            timeout=45, check=False)
    try:
        output = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        output = {"raw_stdout": result.stdout[-1000:]}
    return {"command": command, "returncode": result.returncode,
            "stdout": output, "stderr": result.stderr[-1000:]}


def child(corpus: Path, receipt: Path) -> int:
    e2e = load_e2e()
    from broker.controller_integrations import ControllerIngestion, IntegratedController
    from broker.librarian_completion import build_contract
    from broker.librarian_request import validate_request

    class FileTransport:
        def __init__(self, log: Path):
            self.log = log
        def __call__(self, request):
            from broker.proxy import UpstreamResponse
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"method": request.method, "path": request.path}) + "\n")
            if request.method == "POST":
                body = json.dumps({"operation_ids": ["fixture-interruption-op"]}).encode()
                return UpstreamResponse(202, body)
            return UpstreamResponse(200, b'{"status":"completed"}')

    repo = corpus / "source"
    repo.mkdir(parents=True)
    (repo / "README.md").write_bytes(b"isolated interruption fixture\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    # Reuse the real fixture controller's durable state and prepared-byte path.
    plan = {"docs": {"kind": "git", "repository": str(repo), "commit": commit,
                      "documentation_roots": ["README.md"]}}
    controller = IntegratedController("forced-interruption-fixture", corpus, plan)
    acquired = controller.acquire("docs")
    if not isinstance(acquired, dict) or acquired.get("status") != "complete":
        raise RuntimeError("fixture acquisition did not complete")
    request = validate_request(e2e.REQUEST)
    contract = build_contract(run_id="forced-interruption-fixture", caller_profile="fixture",
                              workspace=corpus, corpus_root=corpus,
                              hindsight_bank="fixture-Liv", ingestion_required=True,
                              requested_products=request["products"])
    transport = FileTransport(corpus / "requests.jsonl")
    ingestion = ControllerIngestion(controller, "fixture-Liv", transport, caller="fixture")
    artifact_id = ingestion.prepare("docs")[0]["id"]
    accepted = ingestion.submit(artifact_id)
    if accepted.get("status") != "accepted":
        raise RuntimeError("fixture submit did not return accepted")
    receipt_data = {"phase": "durable-accepted-receipt", "artifact_id": artifact_id,
                    "operation_id": "fixture-interruption-op", "status": accepted["status"],
                    "commit": commit, "post_count_at_interrupt": 1}
    receipt.write_text(json.dumps(receipt_data, sort_keys=True) + "\n", encoding="utf-8")
    # Parent owns the kill; this is the exact pre-poll interruption point.
    print("receipt-saved", flush=True)
    signal.pause()
    return 0


def fixture_check() -> dict:
    e2e = load_e2e()
    from broker.controller_integrations import ControllerIngestion, IntegratedController

    with tempfile.TemporaryDirectory(prefix="librarian-dev-interruption-") as tmp:
        root = Path(tmp)
        corpus = root / "corpus"
        receipt = root / "accepted-receipt.json"
        child_cmd = [PYTHON, str(Path(__file__).resolve()), "--child",
                     str(corpus), str(receipt)]
        process = subprocess.Popen(child_cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not receipt.exists():
            if process.poll() is not None:
                raise RuntimeError("fixture child exited before receipt")
            time.sleep(0.05)
        if not receipt.exists():
            process.kill()
            process.wait()
            raise RuntimeError("fixture receipt was not durable before timeout")
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        receipt_data = json.loads(receipt.read_text(encoding="utf-8"))
        requests = [json.loads(line) for line in (corpus / "requests.jsonl").read_text().splitlines()]
        posts_before = sum(row["method"] == "POST" for row in requests)

        plan = {"docs": {"kind": "git", "repository": str(corpus / "source"),
                          "commit": receipt_data.get("commit", ""),
                          "documentation_roots": ["README.md"]}}
        recovered_controller = IntegratedController("forced-interruption-fixture", corpus, plan)
        class RecoveryTransport:
            def __call__(self, request):
                from broker.proxy import UpstreamResponse
                with (corpus / "requests.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"method": request.method, "path": request.path}) + "\n")
                return UpstreamResponse(200, b'{"status":"completed"}')
        recovered = ControllerIngestion(recovered_controller, "fixture-Liv", RecoveryTransport(), caller="fixture")
        resumed = recovered.resume(max_submissions=0, max_status_checks=1)
        requests_after = [json.loads(line) for line in (corpus / "requests.jsonl").read_text().splitlines()]
        posts_after = sum(row["method"] == "POST" for row in requests_after)
        return {"status": "passed" if posts_before == posts_after == 1 and resumed.get("checked") == 1 else "failed",
                "scope": "isolated fixture only; not production or dev-bank evidence",
                "child_pid_killed": True, "receipt": receipt_data,
                "posts_before_restart": posts_before, "posts_after_restart": posts_after,
                "no_post_replay": posts_before == posts_after,
                "recovery": resumed}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", nargs=2, metavar=("CORPUS", "RECEIPT"))
    parser.add_argument("--output", type=Path, default=ROOT / "backup-notes/dev-interruption-evidence.json")
    args = parser.parse_args()
    if args.child:
        return child(Path(args.child[0]), Path(args.child[1]))
    run_id = "dev-interruption-check-" + str(os.getpid())
    evidence = {"schema": 1, "supervisor": supervisor_check(run_id),
                "fixture": fixture_check(), "run_id": run_id}
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))
    return 0 if evidence["fixture"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
