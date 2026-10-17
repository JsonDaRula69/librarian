"""Tests for the staged discovery/acquisition boundary probe."""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "staged_acceptance.py"


def run_runner():
    return subprocess.run([sys.executable, str(RUNNER)], cwd=HERE.parents[1],
                          text=True, capture_output=True, check=False)


def test_offline_staged_runner_reports_policy_completeness_blocker_after_acquisition():
    result = run_runner()
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "blocked"
    assert report["mode"] == "offline-fixture"
    assert report["caller"] == report["bank"] == "Liv"
    assert report["stages"]["bank"]["reader"] == "first"
    discovery = report["stages"]["discovery"]
    assert discovery["candidate_learned"] is True
    assert discovery["rpc_sequence"] == [
        "source_list", "discovery_fetch", "discovery_fetch",
        "discovery_register_candidate", "source_propose", "discovery_status",
    ]
    assert discovery["proposal_status"] == "approved"
    assert discovery["acquisition_calls"] > 0
    assert discovery["acquisition_status"] == "complete"
    assert discovery["prepared_text"] > 0
    assert discovery["hashes_verified"] is True
    assert discovery["duplicates"] == 0
    assert discovery["occurrences"] == 1
    assert len(discovery["source_manifest_sha256"]) == 64
    assert len(report["evidence_preserved"]["receipts"]) == 1
    assert len(report["evidence_preserved"]["proposals"]) == 1
    assert next(iter(report["evidence_preserved"]["proposals"].values()))["status"] == "approved"
    assert report["stages"]["launch"]["plan_hashes"] == "missing"
    assert report["blocker"]["code"] == "approved_sources_only"
    assert "acquisition status=complete" in report["blocker"]["detail"]


def test_runner_does_not_emit_production_identity_or_task_purpose():
    result = run_runner()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "purpose" not in result.stdout
    assert "production" not in result.stdout.lower()
    assert "fable-tool" in result.stdout
