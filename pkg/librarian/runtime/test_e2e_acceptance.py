"""Acceptance runner checks; all tests are offline and side-effect scoped."""
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNNER = HERE / "e2e_acceptance.py"
IMAGE = json.loads((HERE / "image.json").read_text())["image_id"]


def run_runner(*args, env=None):
    merged = os.environ.copy()
    merged.pop("LITELLM_API_KEY", None)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(RUNNER), *args], cwd=ROOT,
        env=merged, text=True, capture_output=True, check=False,
    )


def test_offline_runner_completes_connected_chain_without_duplicates():
    result = run_runner()
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "passed"
    assert report["image_id"] == IMAGE
    assert report["mode"] == "offline-fixture"
    assert report["stages"]["relay"]["container_image"] == IMAGE
    ingestion = report["stages"]["ingestion"]
    assert ingestion["ledger_restart"] == "passed"
    assert ingestion["duplicate_submissions"] == 0
    assert ingestion["posts_after_restart"] == ingestion["posts_before_restart"]
    assert report["stages"]["wiring"]["hindsight_post"] == "disabled"
    discovery = report["stages"]["discovery"]
    assert discovery["status"] == "not_claimed"
    assert discovery["candidate_evidence"] == "fetched"
    assert discovery["fetches"] >= 1
    assert discovery["reads"] == 0
    assert discovery["read_required"] is False
    evidence = HERE / "acceptance-evidence" / "acceptance-evidence.json"
    assert evidence.exists()
    assert "not complete corpus" in report["scope"]


def test_live_flag_fails_at_inference_stage_without_host_key():
    result = run_runner("--live-inference")
    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["status"] == "failed"
    assert report["error"].startswith("inference stage:")


def test_secret_is_not_emitted_or_written_by_live_configuration():
    secret = "acceptance-secret-do-not-print"
    result = run_runner(env={"LITELLM_API_KEY": secret})
    assert result.returncode == 0, result.stdout + result.stderr
    assert secret not in result.stdout
    assert secret not in result.stderr
