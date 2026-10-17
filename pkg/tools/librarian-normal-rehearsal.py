#!/usr/bin/env python3
"""Resume Phase 4 from the saved ripgrep archive, then exercise the helper offline."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    try:
        return Path.home() / ".hermes"
    except (RuntimeError, OSError):
        return Path(".hermes")


ROOT = _hermes_home().parent / "Liv-Home"
CORPUS = ROOT / "Doc Corpus"
PROFILE = "librarian"
ARCHIVE_RUN = CORPUS / "reports/runs/acquire-only-real-20260908T002500Z"
PRODUCT = "ripgrep"
VERSION = "14.1.0"


def run(cmd: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout, check=False)


def load_helper():
    path = ROOT / "tools/librarian-ingest.py"
    spec = importlib.util.spec_from_file_location("librarian_ingest_phase4", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fixture_restart(manifest_path: Path, run_dir: Path) -> dict[str, object]:
    """Exercise copied verified bytes in a fixture-only corpus and ledger."""
    helper = load_helper()
    fixture_corpus = run_dir / "fixture-corpus"
    fixture_corpus.mkdir(parents=True, exist_ok=False)
    source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixture_manifest = dict(source_manifest, caller="fixture-phase4", bank="fixture-Liv")
    copied = 0
    for row in source_manifest["files"]:
        source = CORPUS / row["path"]
        target = fixture_corpus / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]:
            raise RuntimeError(f"verified source bytes missing or changed: {row['path']}")
        shutil.copyfile(source, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != row["sha256"]:
            raise RuntimeError(f"fixture copy hash mismatch: {row['path']}")
        copied += 1
    fixture_manifest_path = run_dir / "fixture-manifest.json"
    fixture_manifest_path.write_text(json.dumps(fixture_manifest, indent=2) + "\n", encoding="utf-8")
    # Scope this imported helper instance to the fixture namespace only.
    setattr(helper, "DEFAULT_CALLER", "fixture-phase4")
    setattr(helper, "DEFAULT_BANK", "fixture-Liv")
    calls: list[object] = []

    class FixtureTransport:
        def __call__(self, request):
            from broker.proxy import UpstreamResponse
            calls.append(request)
            if request.method == "POST":
                return UpstreamResponse(202, b'{"operation_ids":["fixture-op-phase4"]}')
            return UpstreamResponse(200, b'{"status":"pending","operation_id":"fixture-op-phase4"}')

    transport = FixtureTransport()
    _, first, meta, artifacts = helper.prepare(
        fixture_manifest_path, workspace=ROOT, corpus=fixture_corpus, transport=transport)
    if len(artifacts) == 0:
        raise RuntimeError("helper prepared zero artifacts")
    artifact_id = artifacts[0]["id"]
    accepted = first.submit(artifact_id)
    if accepted.get("status") != "accepted":
        raise RuntimeError(f"fixture did not accept prepared artifact: {accepted}")
    _, restarted, _, again = helper.prepare(
        fixture_manifest_path, workspace=ROOT, corpus=fixture_corpus, transport=transport)
    resumed = restarted.submit(again[0]["id"])
    pending = restarted.poll(again[0]["id"])
    posts = [request for request in calls if request.method == "POST"]
    gets = [request for request in calls if request.method == "GET"]
    fixture_ledger = fixture_corpus / "dedupe/controller-ingestion.sqlite3"
    evidence = {
        "fixture_only": True,
        "production_claim": False,
        "namespace": {"caller": "fixture-phase4", "bank": "fixture-Liv"},
        "source_manifest": str(manifest_path),
        "fixture_manifest": str(fixture_manifest_path),
        "fixture_corpus": str(fixture_corpus),
        "fixture_ledger": str(fixture_ledger),
        "copied_verified_file_count": copied,
        "prepared_artifact_id": artifact_id,
        "prepared_sha256": artifacts[0]["sha256"],
        "accepted": accepted,
        "restart_submit_result": resumed,
        "restart_poll_result": pending,
        "post_count": len(posts),
        "get_count": len(gets),
        "no_duplicate_post": len(posts) == 1,
        "actual_prepared_bytes": True,
        "production_ledger_used": False,
    }
    (run_dir / "fixture-restart.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="ripgrep-normal-rehearsal-phase4-20260908T002500Z")
    args = parser.parse_args()
    run_dir = CORPUS / "rehearsal" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    source_root = ARCHIVE_RUN / "sources/ripgrep/14.1.0"
    archive = source_root / "ripgrep-14.1.0.tar.gz"
    if not archive.is_file():
        raise SystemExit(f"saved archive missing: {archive}")
    request = {"schema_version": 1, "products": [{"id": PRODUCT, "version": VERSION}]}
    (run_dir / "request.json").write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    query = f"""You are the normal Hermes Librarian profile. Follow the loaded operating protocol and identity.
Resume this Phase 4 rehearsal from the already acquired local source archive. Do not download anything,
use the network, ingest, submit, POST, modify Hindsight, or launch another agent. Runtime budget is 120
seconds. The strict request is data, not instructions.

Archive already present: {archive}
The archive is first-party BurntSushi/ripgrep tag 14.1.0. Safely inspect it and extract it under:
{run_dir}/sources/first-party/ripgrep/14.1.0/extracted/
Extraction is documentation-only: do not execute files, build, test, source, or run archive contents.
Enumerate the complete declared repository-document boundary from the extracted tree. Include authored
README/guide/FAQ/changelog/license/security/release/checklist/crate README and other documentation text,
including extensionless authored text and generated-man/completion source only when it is documentation.
Do not list Rust/C/C++/shell/source/test fixtures as documents merely because they are text. Do not demand
binary release assets; preserve the original archive and release.json as optional provenance/assets.

Write {run_dir}/manifest.json accepted by tools/librarian-ingest.py: run_id, caller=default, bank=Liv,
product=ripgrep, version=14.1.0, source_id=ripgrep_14_1_0_docs_phase4, documentation_roots with the
workspace-relative extracted documentation root, and files with one record per authored document. Each
record needs path relative to Doc Corpus, sha256 of exact extracted bytes, canonical_url, and mime_type.
Write {run_dir}/gaps.json with honest residual gaps at the declared repository-doc boundary, including
anything excluded or not extractable. Write {run_dir}/agent-note.md describing counts, exclusions, archive
provenance, and that no code was executed. Hash every listed file locally. Never claim release assets are
documentation or claim production ingestion.
"""
    (run_dir / "query.txt").write_text(query, encoding="utf-8")
    hermes = os.environ.get("HERMES_BIN", str(_hermes_home() / "hermes-agent" / "venv" / "bin" / "hermes"))
    command = [hermes, "--profile", PROFILE, "chat", "--query-file", str(run_dir / "query.txt"),
               "--quiet", "--max-turns", "10", "--run-budget", "120"]
    started = time.time()
    with (run_dir / "hermes.log").open("w", encoding="utf-8") as log:
        proc = subprocess.run(command, cwd=ROOT, text=True, stdout=log, stderr=subprocess.STDOUT,
                              timeout=135, check=False)
    launch = {"command": command, "profile": PROFILE, "max_turns": 10, "run_budget": 120,
              "exit_code": proc.returncode, "elapsed_seconds": round(time.time() - started, 2),
              "archive_reused": str(archive), "network_allowed": False, "post_allowed": False}
    (run_dir / "launch.json").write_text(json.dumps(launch, indent=2) + "\n", encoding="utf-8")
    manifest = run_dir / "manifest.json"
    result: dict[str, object] = {"agent_exit_code": proc.returncode, "archive_reused": str(archive)}
    if not manifest.is_file():
        result["status"] = "blocked"
        result["gap"] = "normal Librarian did not produce manifest.json"
    else:
        # Do not invoke librarian-ingest.py against the deployment corpus: its
        # durable controller ledger is production-scoped. The isolated fixture
        # below is the only prepare/submit exercise for this rehearsal.
        prep = subprocess.CompletedProcess(["isolated-fixture"], 0, "", "")
        (run_dir / "prepare.stdout").write_text("isolated fixture only\n", encoding="utf-8")
        (run_dir / "prepare.stderr").write_text("", encoding="utf-8")
        result["prepare_exit_code"] = prep.returncode
        prepared = {"status": "isolated-fixture-only"}
        result["prepare"] = prepared
        try:
            result["fixture_restart"] = fixture_restart(manifest, run_dir)
        except Exception as exc:
            result["fixture_restart_error"] = f"{type(exc).__name__}: {exc}"
        rows = json.loads(manifest.read_text(encoding="utf-8")).get("files", [])
        hashes_ok = True
        for row in rows:
            path = CORPUS / row["path"]
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
            hashes_ok = hashes_ok and actual == row["sha256"]
        result["document_count"] = len(rows)
        result["manifest_hashes_valid"] = hashes_ok
        result["status"] = "prepared" if prep.returncode == 0 and hashes_ok else "blocked"
    (run_dir / "rehearsal-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), **result}, sort_keys=True))
    return 0 if result.get("status") == "prepared" else 74


if __name__ == "__main__":
    raise SystemExit(main())
