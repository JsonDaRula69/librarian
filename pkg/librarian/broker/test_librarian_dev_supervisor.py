"""Focused tests for the supervisor's bounded isolated-dev connector."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("launcher", Path(__file__).parents[1] / "tools/librarian-launch.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_dev_connector_claims_and_cleans_owner(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    corpus.mkdir()
    manifest = workspace / "manifest.json"
    manifest.write_text("{}")
    calls = []

    class Result:
        returncode = 0
        stdout = '{"status":"status-only"}'

    def run(command, **kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(launcher.subprocess, "run", run)
    result = launcher.run_dev_helper(manifest=manifest, workspace=workspace,
                                     corpus_root=corpus, action="resume",
                                     allow_submit=False, run_id="run", timeout=5)
    assert result["status"] == "completed"
    assert "--dev-instance" in calls[0]
    assert all("--allow-submit" not in command for command in calls)
    assert json.loads((corpus / "reports/runs/run/dev-stage-receipt.json").read_text())["run_budget"] == 5
    with launcher.RunClaimStore(corpus / "dev-hindsight/dedupe/live-trial-claims.sqlite")._connect() as db:
        assert db.execute("select count(*) from run_claims").fetchone()[0] == 0


def test_dev_duplicate_claim_does_not_start_helper(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    corpus.mkdir()
    manifest = workspace / "manifest.json"
    manifest.write_text("{}")
    (corpus / "dev-hindsight/dedupe").mkdir(parents=True)
    store = launcher.RunClaimStore(corpus / "dev-hindsight/dedupe/live-trial-claims.sqlite")
    fp = launcher.scope_fingerprint("dev-manifest", str(manifest))
    store.claim("default:Liv:dev-instance", "manifest.json", "continuation", fp,
                "other-owner", "existing", 9999999999, 300)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: pytest.fail("helper started"))
    result = launcher.run_dev_helper(manifest=manifest, workspace=workspace,
                                     corpus_root=corpus, action="resume",
                                     allow_submit=False, run_id="run", timeout=5)
    assert result["status"] == "attach-existing"
    assert result["existing_run_id"] == "existing"


def test_dev_manifest_must_be_workspace_owned(tmp_path):
    corpus = tmp_path / "Doc Corpus"
    corpus.mkdir()
    outside = tmp_path.parent / "outside-manifest.json"
    outside.write_text("{}")
    with pytest.raises(launcher.BoundaryError, match="inside"):
        launcher.run_dev_helper(manifest=outside, workspace=tmp_path,
                                corpus_root=corpus, action="status",
                                allow_submit=False, run_id="run", timeout=5)
