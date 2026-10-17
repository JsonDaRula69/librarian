import importlib.util
import json
from pathlib import Path


LAUNCHER = Path(__file__).resolve().parents[1] / "tools" / "librarian-launch.py"
spec = importlib.util.spec_from_file_location("librarian_launcher_acquire", LAUNCHER)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_acquisition_prompt_loads_canonical_protocol_and_component_contract(tmp_path):
    prompt = launcher.build_acquisition_prompt(
        request={"schema_version": 1, "products": [{"id": "ripgrep", "version": "14"}]},
        run_dir=tmp_path / "run", workspace=tmp_path, corpus_root=tmp_path / "corpus",
    )
    assert "CANONICAL OPERATING PROTOCOL" in prompt
    assert "Independently establish software identity" in prompt
    assert "relevant operational dependencies" in prompt
    assert "component-manifest-index.json" in prompt
    assert "caller=default, bank=Liv" in prompt
    assert "POST" in prompt
    assert "Do not record offline Hindsight access as an acquisition gap" in prompt


def test_acquisition_prepares_manifest_and_keeps_status_distinct(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    corpus.mkdir()
    request = {"schema_version": 1, "products": [{"id": "ripgrep", "version": "14"}]}
    run_dir = corpus / "reports" / "runs" / "run-1"

    def fake_executor(cmd, cwd, env, output, timeout=None):
        run_dir.mkdir(parents=True, exist_ok=True)
        data = b"first-party README\n"
        source = run_dir / "sources" / "first-party" / "ripgrep" / "README.md"
        source.parent.mkdir(parents=True)
        source.write_bytes(data)
        import hashlib
        (run_dir / "manifest.json").write_text(json.dumps({
            "run_id": "run-1", "caller": "default", "bank": "Liv",
            "product": "ripgrep", "version": "14", "source_id": "ripgrep_docs",
            "documentation_roots": ["reports/runs/run-1/sources/first-party/ripgrep"],
            "files": [{"path": "reports/runs/run-1/sources/first-party/ripgrep/README.md",
                       "sha256": hashlib.sha256(data).hexdigest(),
                       "canonical_url": "https://github.com/BurntSushi/ripgrep"}],
        }) + "\n")
        (run_dir / "gaps.json").write_text(json.dumps({
            "status": "complete", "semantic_status": "complete", "gaps": [],
        }))
        return 0, False

    monkeypatch.setattr(launcher, "run_executor", fake_executor)
    monkeypatch.setattr(launcher, "run_acquisition_evidence_gate",
                        lambda *args: {"ok": True, "run_id": "run-1"})
    class Prep:
        returncode = 0
        stdout = '{"status": "prepared", "artifacts": [{"id": "artifact-1"}]}\n'
        stderr = ''
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: Prep())
    result = launcher.run_normal_acquisition(
        request=request, run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id="run-1", timeout=5, max_turns=30,
    )
    assert result["agent_exit_code"] == 0
    assert result["prepared_status"] == "prepared"
    assert result["valid"] is True
    assert result["status"] == "completed"
    assert result["acquisition_status"] == "complete"
    assert result["prepare"]["status"] == "prepared"
    assert json.loads((run_dir / "launch.json").read_text())["mode"] == "acquire-only"
    assert "--max-turns" in json.loads((run_dir / "launch.json").read_text())["command"]
    assert json.loads((run_dir / "launch.json").read_text())["max_turns"] == 30


def test_acquire_only_rejects_non_default_scope(tmp_path):
    request = {"schema_version": 1, "products": [{"id": "x", "version": "1"}]}
    try:
        launcher.run_normal_acquisition(
            request=request, run_dir=tmp_path / "run", workspace=tmp_path,
            corpus_root=tmp_path, caller_profile="librarian", bank="Liv",
            run_id="run", timeout=1,
        )
    except ValueError as exc:
        assert "default" in str(exc)
    else:
        raise AssertionError("scope must be rejected")


def test_acquisition_resume_is_passed_and_preserves_prior_receipt(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    corpus.mkdir()
    run_dir = corpus / "reports" / "runs" / "acquire-router-fix-20260908T010000Z"
    run_dir.mkdir(parents=True)
    (run_dir / "launch.json").write_text('{"old": true}\n')
    # An interrupted run leaves the regenerated executor guard bin behind; the
    # resume snapshot must not copy it into the attempt directory.
    stale_guard = run_dir / ".executor-bin-live"
    stale_guard.mkdir()
    (stale_guard / "librarian").write_text("#!/bin/sh\nexit 99\n")
    request = {"schema_version": 1, "products": [{"id": "ripgrep", "version": "14"}]}

    def fake_executor(cmd, cwd, env, output, timeout=None):
        return 1, False

    monkeypatch.setattr(launcher, "run_executor", fake_executor)
    result = launcher.run_normal_acquisition(
        request=request, run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id=run_dir.name, timeout=5,
        max_turns=30, resume_session="session20260908_015134_d24ead",
    )
    receipt = json.loads((run_dir / "launch.json").read_text())
    assert receipt["resume_session"] == "session20260908_015134_d24ead"
    assert "--resume" in receipt["command"]
    assert receipt["command"][receipt["command"].index("--resume") + 1] == "session20260908_015134_d24ead"
    attempt_root = run_dir / "resume-attempts" / "session20260908_015134_d24ead"
    preserved = list(attempt_root.glob("*/launch.json"))
    assert len(preserved) == 1
    assert json.loads(preserved[0].read_text()) == {"old": True}
    attempt_dirs = list(attempt_root.iterdir())
    assert len(attempt_dirs) == 1
    guard = attempt_dirs[0] / ".executor-bin-live" / "librarian"
    assert guard.is_file() and "recursive Librarian launch blocked" in guard.read_text()


def test_acquisition_never_prepares_when_evidence_gate_fails(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    corpus.mkdir()
    run_dir = corpus / "reports/runs/run-gate-fail"
    request = {"schema_version": 1, "products": [{"id": "widget", "version": "1"}]}

    def fake_executor(cmd, cwd, env, output, timeout=None):
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text("{}\n")
        (run_dir / "gaps.json").write_text('{"status":"partial","gaps":["missing"]}\n')
        return 0, False

    monkeypatch.setattr(launcher, "run_executor", fake_executor)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("prepare must not run after a failed evidence gate")))

    result = launcher.run_normal_acquisition(
        request=request, run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id=run_dir.name, timeout=5,
    )

    assert result["prepared_status"] == "evidence-gate-failed"
    assert result["prepare_exit_code"] == 2
    assert result["evidence_gate"]["ok"] is False


def test_generic_reconciliation_repairs_only_path_shapes(tmp_path):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    run = corpus / "reports/runs/run"
    source = run / "sources/widget/README.md"
    source.parent.mkdir(parents=True)
    source.write_text("docs\n")
    manifest = run / "manifest.json"
    manifest.write_text(json.dumps({
        "corpus_root": str(workspace),
        "documentation_roots": ["reports/runs/run/sources/widget"],
        "files": [{"path": "Doc Corpus/reports/runs/run/sources/widget/README.md"}],
    }))
    index = run / "component-manifest-index.json"
    index.write_text(json.dumps({"manifests": [str(manifest)]}))

    receipt = launcher.reconcile_acquisition_artifact_paths(run, corpus, workspace)

    repaired = json.loads(manifest.read_text())
    repaired_index = json.loads(index.read_text())
    assert receipt["changed"] == ["component-manifest-index.json", "manifest.json"]
    assert repaired["corpus_root"] == str(corpus)
    assert repaired["files"][0]["path"] == "reports/runs/run/sources/widget/README.md"
    assert repaired_index["manifests"] == ["manifest.json"]
    assert "source_enumeration" not in repaired


def test_generic_reconciliation_repairs_listed_component_manifests(tmp_path):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    run = corpus / "reports/runs/run"
    source = run / "sources/dependency/README.md"
    source.parent.mkdir(parents=True)
    source.write_text("dependency docs\n")
    (run / "manifest.json").write_text(json.dumps({"files": []}))
    component = run / "manifest-dependency.json"
    component.write_text(json.dumps({
        "corpus_root": str(workspace),
        "files": [{"path": "Doc Corpus/reports/runs/run/sources/dependency/README.md"}],
    }))
    (run / "component-manifest-index.json").write_text(json.dumps({
        "manifests": ["manifest.json", "manifest-dependency.json"],
    }))

    receipt = launcher.reconcile_acquisition_artifact_paths(run, corpus, workspace)

    repaired = json.loads(component.read_text())
    assert "manifest-dependency.json" in receipt["changed"]
    assert repaired["corpus_root"] == str(corpus)
    assert repaired["files"][0]["path"] == "reports/runs/run/sources/dependency/README.md"


def test_acquisition_gate_failure_runs_bounded_correction(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    corpus.mkdir()
    run_dir = corpus / "reports/runs/run-correct"
    request = {"schema_version": 1, "products": [{"id": "widget", "version": "1"}]}
    calls = []

    def fake_executor(cmd, cwd, env, output, timeout=None):
        calls.append(cmd)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text("{}\n")
        gaps = ({"status": "partial"} if len(calls) == 1
                else {"status": "complete", "semantic_status": "complete", "gaps": []})
        (run_dir / "gaps.json").write_text(json.dumps(gaps) + "\n")
        return 0, False

    gates = iter([
        {"ok": False, "error_type": "GateError", "reason": "missing semantic accounting"},
        {"ok": True, "run_id": "run-correct"},
    ])
    monkeypatch.setattr(launcher, "run_executor", fake_executor)
    monkeypatch.setattr(launcher, "run_acquisition_evidence_gate", lambda *args: next(gates))
    class Prep:
        returncode = 0
        stdout = '{"status":"prepared","artifacts":[]}\n'
        stderr = ""
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: Prep())

    result = launcher.run_normal_acquisition(
        request=request, run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id=run_dir.name, timeout=5,
        correction_attempts=1,
    )

    assert len(calls) == 2
    assert result["evidence_gate"]["ok"] is True
    assert result["prepared_status"] == "prepared"
    assert result["gaps"]["status"] == "complete"
    assert (run_dir / "acquisition-gate-correction-1.json").is_file()


def test_repaired_run_survives_nonzero_correction_exit(tmp_path, monkeypatch):
    """A correction session that repairs every artifact and then exits non-zero (its own turn
    cap) must still be judged by the re-evaluated gate, not by the stale pre-correction verdict
    and not by the session exit code."""
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    corpus.mkdir()
    run_dir = corpus / "reports/runs/run-capped-correct"
    request = {"schema_version": 1, "products": [{"id": "widget", "version": "1"}]}
    calls = []

    def fake_executor(cmd, cwd, env, output, timeout=None):
        calls.append(cmd)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text("{}\n")
        gaps = ({"status": "partial"} if len(calls) == 1
                else {"status": "complete", "semantic_status": "complete", "gaps": []})
        (run_dir / "gaps.json").write_text(json.dumps(gaps) + "\n")
        return 1, False  # both sessions end non-zero, exactly like the 2026-10-02 Discord run

    gates = iter([
        {"ok": False, "error_type": "GateError", "reason": "missing evidence"},
        {"ok": True, "run_id": "run-capped-correct"},
    ])
    monkeypatch.setattr(launcher, "run_executor", fake_executor)
    monkeypatch.setattr(launcher, "run_acquisition_evidence_gate", lambda *args: next(gates))

    class Prep:
        returncode = 0
        stdout = '{"status":"prepared","artifacts":[]}\n'
        stderr = ""

    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: Prep())

    result = launcher.run_normal_acquisition(
        request=request, run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id=run_dir.name, timeout=5,
        correction_attempts=1,
    )

    assert len(calls) == 2
    assert result["agent_exit_code"] == 1
    # Behaviour first: the re-evaluated gate decides, so the repaired run prepares.
    assert result["evidence_gate"]["ok"] is True
    assert result["prepared_status"] == "prepared"
    assert result["last_agent_exit_code"] == 1


def test_structural_gate_cannot_promote_partial_semantic_gaps(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    corpus.mkdir()
    run_dir = corpus / "reports/runs/run-partial"
    request = {"schema_version": 1, "products": [{"id": "widget", "version": "1"}]}

    def fake_executor(cmd, cwd, env, output, timeout=None):
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text("{}\n")
        (run_dir / "gaps.json").write_text(json.dumps({
            "status": "partial", "semantic_status": "partial",
            "gaps": [{"surface": "manual", "status": "unresolved"}],
        }) + "\n")
        return 0, False

    monkeypatch.setattr(launcher, "run_executor", fake_executor)
    monkeypatch.setattr(launcher, "run_acquisition_evidence_gate",
                        lambda *args: {"ok": True, "run_id": "run-partial"})
    class Prep:
        returncode = 0
        stdout = '{"status":"prepared","artifacts":[]}\n'
        stderr = ""
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: Prep())

    result = launcher.run_normal_acquisition(
        request=request, run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id=run_dir.name, timeout=5,
        correction_attempts=0,
    )

    assert result["valid"] is False
    assert result["status"] == "partial"
    assert result["acquisition_status"] == "partial"
    assert result["prepared_status"] == "prepared"


def test_partial_semantic_gaps_trigger_bounded_correction(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    corpus.mkdir()
    run_dir = corpus / "reports/runs/run-semantic-correct"
    request = {"schema_version": 1, "products": [{"id": "widget", "version": "1"}]}
    calls = []

    def fake_executor(cmd, cwd, env, output, timeout=None):
        calls.append(cmd)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text("{}\n")
        gaps = ({"status": "partial", "semantic_status": "partial", "gaps": [{"status": "unresolved"}]}
                if len(calls) == 1 else
                {"status": "complete", "semantic_status": "complete", "gaps": []})
        (run_dir / "gaps.json").write_text(json.dumps(gaps) + "\n")
        return 0, False

    monkeypatch.setattr(launcher, "run_executor", fake_executor)
    monkeypatch.setattr(launcher, "run_acquisition_evidence_gate",
                        lambda *args: {"ok": True, "run_id": "run-semantic-correct"})
    class Prep:
        returncode = 0
        stdout = '{"status":"prepared","artifacts":[]}\n'
        stderr = ""
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: Prep())

    result = launcher.run_normal_acquisition(
        request=request, run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id=run_dir.name, timeout=5,
        correction_attempts=1,
    )

    assert len(calls) == 2
    assert result["valid"] is True
    assert result["gaps"]["status"] == "complete"


def test_correction_reloads_new_component_manifest_index(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    corpus.mkdir()
    run_dir = corpus / "reports/runs/run-components"
    request = {"schema_version": 1, "products": [{"id": "widget", "version": "1"}]}
    calls = []
    prepared = []

    def fake_executor(cmd, cwd, env, output, timeout=None):
        calls.append(cmd)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text("{}\n")
        if len(calls) == 1:
            gaps = {"status": "partial", "semantic_status": "partial", "gaps": [{"status": "unresolved"}]}
        else:
            (run_dir / "manifest-dependency.json").write_text("{}\n")
            (run_dir / "component-manifest-index.json").write_text(json.dumps({
                "manifests": ["manifest.json", "manifest-dependency.json"],
            }) + "\n")
            gaps = {"status": "complete", "semantic_status": "complete", "gaps": []}
        (run_dir / "gaps.json").write_text(json.dumps(gaps) + "\n")
        return 0, False

    monkeypatch.setattr(launcher, "run_executor", fake_executor)
    monkeypatch.setattr(launcher, "run_acquisition_evidence_gate",
                        lambda *args: {"ok": len(calls) > 1, "run_id": "run-components"})
    class Prep:
        returncode = 0
        stdout = '{"status":"prepared","artifacts":[]}\n'
        stderr = ""
    def fake_prepare(args, **kwargs):
        prepared.append(Path(args[3]).name)
        return Prep()
    monkeypatch.setattr(launcher.subprocess, "run", fake_prepare)

    result = launcher.run_normal_acquisition(
        request=request, run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id=run_dir.name, timeout=5,
        correction_attempts=1,
    )

    assert result["valid"] is True
    assert prepared == ["manifest.json", "manifest-dependency.json"]


def test_normal_workflow_reuses_verified_bank_content_without_submit(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    run_dir = corpus / "reports/runs/normal-reuse"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "manifest.json"
    manifest.write_text("{}\n")
    monkeypatch.setattr(launcher, "run_normal_acquisition", lambda **kwargs: {
        "valid": True, "status": "completed", "manifest": str(manifest),
        "prepared_status": "prepared",
    })
    commands = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps({
            "status": "read-only",
            "results": [{"bank": {"disposition": "verified-present"},
                         "bank_coverage": {"complete": True, "has_more": False}}],
        })

    def fake_run(args, **kwargs):
        commands.append(args)
        return Result()

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    result = launcher.run_normal_workflow(
        request={"schema_version": 1, "products": [{"id": "widget", "version": "1"}]},
        run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id=run_dir.name, timeout=5,
    )

    assert result["valid"] is True
    assert result["ingestion_status"] == "verified-present"
    assert [command[2] for command in commands] == ["status"]
    assert all("submit" not in command for command in commands)


def test_normal_workflow_requires_explicit_submit_authorization(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    run_dir = corpus / "reports/runs/normal-missing"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "manifest.json"
    manifest.write_text("{}\n")
    monkeypatch.setattr(launcher, "run_normal_acquisition", lambda **kwargs: {
        "valid": True, "status": "completed", "manifest": str(manifest),
        "prepared_status": "prepared",
    })
    commands = []
    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"status": "read-only", "results": [{
            "bank": {"disposition": "missing"},
            "bank_coverage": {"complete": True, "has_more": False},
        }]})
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda args, **kwargs: commands.append(args) or Result())

    result = launcher.run_normal_workflow(
        request={"schema_version": 1, "products": [{"id": "widget", "version": "1"}]},
        run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id=run_dir.name, timeout=5,
        allow_submit=False,
    )

    assert result["valid"] is False
    assert result["status"] == "ingestion-pending"
    assert result["ingestion_status"] == "submission-not-authorized"
    assert len(commands) == 1


def test_normal_workflow_rejects_unknown_bank_without_submit(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    run_dir = corpus / "reports/runs/normal-unknown"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "manifest.json"
    manifest.write_text("{}\n")
    monkeypatch.setattr(launcher, "run_normal_acquisition", lambda **kwargs: {
        "valid": True, "status": "completed", "manifest": str(manifest),
        "prepared_status": "prepared",
    })
    commands = []
    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"status": "read-only", "results": [{
            "bank": {"disposition": "unknown"},
            "bank_coverage": {"complete": False, "has_more": True},
        }]})
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda args, **kwargs: commands.append(args) or Result())

    result = launcher.run_normal_workflow(
        request={"schema_version": 1, "products": [{"id": "widget", "version": "1"}]},
        run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id=run_dir.name, timeout=5,
        allow_submit=True, inventory_status_attempts=1,
    )

    assert result["valid"] is False
    assert result["status"] == "reconciliation-required"
    assert len(commands) == 1


def test_normal_workflow_advances_bounded_inventory_until_complete(tmp_path, monkeypatch):
    workspace = tmp_path
    corpus = workspace / "Doc Corpus"
    run_dir = corpus / "reports/runs/normal-progress"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "manifest.json"
    manifest.write_text("{}\n")
    monkeypatch.setattr(launcher, "run_normal_acquisition", lambda **kwargs: {
        "valid": True, "status": "completed", "manifest": str(manifest),
        "prepared_status": "prepared",
    })
    responses = iter([
        {"status": "read-only", "results": [{
            "bank": {"disposition": "unknown"},
            "bank_coverage": {"complete": False, "has_more": True, "offset": 100},
        }]},
        {"status": "read-only", "results": [{
            "bank": {"disposition": "verified-present"},
            "bank_coverage": {"complete": True, "has_more": False, "offset": 200},
        }]},
    ])
    commands = []
    class Result:
        returncode = 0
        stderr = ""
        stdout = ""
    def fake_run(args, **kwargs):
        commands.append(args)
        result = Result()
        result.stdout = json.dumps(next(responses))
        return result
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    result = launcher.run_normal_workflow(
        request={"schema_version": 1, "products": [{"id": "widget", "version": "1"}]},
        run_dir=run_dir, workspace=workspace, corpus_root=corpus,
        caller_profile="default", bank="Liv", run_id=run_dir.name, timeout=5,
        inventory_status_attempts=2,
    )

    assert result["valid"] is True
    assert result["ingestion_status"] == "verified-present"
    assert len(commands) == 2
