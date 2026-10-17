"""Focused connector tests using a real PTY executable, not model callbacks."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).parents[1] / "tools" / "librarian-launch.py"
spec = importlib.util.spec_from_file_location("librarian_launcher_native", LAUNCHER)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

FIXTURE = r'''#!/usr/bin/env python3
import os
import sys
import time
os.write(1, b"librarian ")
os.write(1, "❯".encode("utf-8"))
for raw in sys.stdin:
    command = raw.strip()
    if command == "/goal status":
        os.write(1, b"\x1b[2K\xe2\x8f\xb8 Goal (paused, 0/30 turns): paused\r\n")
        os.write(1, "librarian ❯".encode())
    elif command.startswith("/goal gate add"):
        # Split bytes and ANSI prove matching is incremental and UI-normalized.
        text = "⚿ Gate added: $ " + command[len("/goal gate add "):] + " (3 retries, 300s timeout). It must pass before the goal can complete.\r\n"
        data = b"\x1b[1m" + text.encode("utf-8") + b"\x1b[0m"
        for byte in data:
            os.write(1, bytes([byte]))
        os.write(1, "librarian ❯".encode())
    elif command == "/goal resume":
        os.write(1, "✓ Goal achieved: paused\r\n".encode("utf-8"))
        break
'''


def executable(tmp_path, body=FIXTURE, name="fake-hermes"):
    path = tmp_path / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def verifier(tmp_path, body="#!/bin/sh\nexit 0\n"):
    return executable(tmp_path, body, "verifier")


def run_native(tmp_path, **kwargs):
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    return launcher.run_native_goal(
        run_dir=workspace / "run", workspace=workspace,
        resume_session=kwargs.pop("resume_session", "paused-session"),
        timeout=kwargs.pop("timeout", 3), executable=str(kwargs.pop("executable", executable(tmp_path))),
        verifier_argv=kwargs.pop("verifier_argv", [str(verifier(tmp_path))]), **kwargs,
    )


def test_native_goal_requires_real_ack_and_verifier(tmp_path):
    result = run_native(tmp_path)
    assert result["native_terminal"] is True
    assert result["verifier_passed"] is True
    assert result["cleanup_verified"] is True
    assert result["status"] == "completed"
    receipt = json.loads((tmp_path / "workspace with spaces" / "run" / "native-goal-receipt.json").read_text())
    assert receipt["command"][-5:] == ["--resume", "paused-session", "--no-restore-cwd", "--in", str(tmp_path / "workspace with spaces")]
    assert receipt["status"] == "completed"
    assert not (tmp_path / "workspace with spaces" / "run" / "result.json").exists()


def test_malformed_gate_echo_is_not_acknowledgement(tmp_path):
    body = r"""#!/usr/bin/env python3
import os, sys
for raw in sys.stdin:
    command = raw.strip()
    if command == "/goal status": os.write(1, "⏸ Goal (paused, 0/30 turns): paused\r\n".encode())
    elif command.startswith("/goal gate add"): os.write(1, b"gate acknowledged\r\n")
"""
    result = run_native(tmp_path, executable=executable(tmp_path, body, "counterfeit"), timeout=0.5)
    assert result["native_terminal"] is False
    assert result["verifier_passed"] is False
    assert result["status"] in {"partial", "timeout", "blocked"}


def test_verifier_timeout_is_bounded_and_not_completed(tmp_path):
    slow = executable(tmp_path, "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n", "slow-verifier")
    result = run_native(tmp_path, timeout=0.5, verifier_argv=[str(slow)])
    assert result["native_terminal"] is True
    assert result["verifier_passed"] is False
    assert result["status"] in {"timeout", "partial"}


def test_verifier_argv_rejects_control_bytes_and_missing_value():
    with pytest.raises(ValueError):
        launcher._validate_native_verifier(["ok\nno"])
    with pytest.raises(ValueError):
        launcher._validate_native_verifier([])


def test_cli_requires_native_verifier_argv(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    proc = subprocess.run([
        sys.executable, str(LAUNCHER), "--native-goal", "--resume-session", "paused",
        "--caller-profile", "default", "--workspace", str(workspace), "--hindsight-bank", "Liv",
    ], capture_output=True, text=True)
    assert proc.returncode == 2
    assert "--native-verifier-argv" in proc.stderr


def test_cli_accepts_fresh_native_goal_without_resume_session(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    proc = subprocess.run([
        sys.executable, str(LAUNCHER), "--native-goal", "--native-goal-text", "fresh objective",
        "--caller-profile", "default", "--workspace", str(workspace), "--hindsight-bank", "Liv",
    ], capture_output=True, text=True)
    assert proc.returncode == 2
    assert proc.stderr.rstrip().endswith("--native-goal requires --native-verifier-argv")


def test_cli_rejects_fresh_and_resume_native_goal_together(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    proc = subprocess.run([
        sys.executable, str(LAUNCHER), "--native-goal", "--native-goal-text", "fresh objective",
        "--resume-session", "existing", "--native-verifier-argv", '["true"]',
        "--caller-profile", "default", "--workspace", str(workspace), "--hindsight-bank", "Liv",
    ], capture_output=True, text=True)
    assert proc.returncode == 2
    assert "exactly one" in proc.stderr


def test_delayed_prompt_drops_early_input(tmp_path):
    body = r'''#!/usr/bin/env python3
import os, sys, time
time.sleep(0.15)
os.write(1, "librarian ❯".encode())
for raw in sys.stdin:
    if raw.strip() == "/goal status":
        os.write(1, "⏸ Goal (paused, 0/30 turns): paused\r\nlibrarian ❯".encode())
    elif raw.strip().startswith("/goal gate add"):
        os.write(1, ("⚿ Gate added: $ " + raw.strip()[16:] + " (3 retries, 300s timeout). It must pass before the goal can complete.\r\nlibrarian ❯").encode())
    elif raw.strip() == "/goal resume":
        os.write(1, "✓ Goal achieved: done\r\n".encode())
        break
'''
    result = run_native(tmp_path, executable=executable(tmp_path, body, "delayed"))
    assert result["status"] in {"completed", "partial", "timeout"}


def test_done_goal_is_blocked_without_gate_or_resume(tmp_path):
    body = r'''#!/usr/bin/env python3
import os, sys
os.write(1, "librarian ❯".encode())
for raw in sys.stdin:
    if raw.strip() == "/goal status":
        os.write(1, "✓ Goal done: existing\r\nlibrarian ❯".encode())
        break
'''
    result = run_native(tmp_path, executable=executable(tmp_path, body, "done"), timeout=1)
    assert result["status"] == "blocked"
    assert result["native_terminal"] is False


def test_fresh_goal_is_set_paused_gated_and_resumed(tmp_path):
    body = r'''#!/usr/bin/env python3
import os, sys
os.write(1, "librarian ❯".encode())
for raw in sys.stdin:
    command = raw.strip()
    if command == "/goal pause":
        os.write(1, "⏸ Goal paused: fresh objective\r\nlibrarian ❯".encode())
    elif command == "/goal status":
        os.write(1, "⏸ Goal (paused, 0/20 turns): fresh objective\r\nlibrarian ❯".encode())
    elif command.startswith("/goal gate add"):
        os.write(1, ("⚿ Gate added: $ " + command[len("/goal gate add "):] + " (3 retries, 300s timeout). It must pass before the goal can complete.\r\nlibrarian ❯").encode())
    elif command == "/goal resume":
        os.write(1, "✓ Goal achieved: verified\r\n".encode())
        break
    elif command.startswith("/goal "):
        goal = command[len("/goal "):]
        # The kickoff is already busy, so no prompt appears here.
        os.write(1, ("⊙ Goal set (20-turn budget): " + goal + "\r\n").encode())
'''
    result = run_native(
        tmp_path, executable=executable(tmp_path, body, "fresh"),
        resume_session=None, goal_text="fresh objective",
    )
    assert result["goal_initialized"] is True
    assert result["goal_paused"] is True
    assert result["native_terminal"] is True
    assert result["status"] == "completed"


def test_fresh_goal_requires_nonempty_text(tmp_path):
    with pytest.raises(ValueError, match="goal text"):
        run_native(tmp_path, resume_session=None, goal_text="")


def test_native_goal_receipt_records_real_session_id(tmp_path):
    body = FIXTURE.replace(
        'os.write(1, b"librarian ")',
        'os.write(1, b"Session: 20260919_120000_abcdef\\r\\n")\nos.write(1, b"librarian ")',
    )
    result = run_native(tmp_path, executable=executable(tmp_path, body, "session-id"))
    assert result["session_id"] == "20260919_120000_abcdef"


def test_native_goal_runs_preflight_before_starting_hermes(tmp_path):
    marker = tmp_path / "started"
    hermes = executable(tmp_path, f"#!/bin/sh\ntouch '{marker}'\nexit 0\n", "must-not-start")
    failed = executable(tmp_path, "#!/bin/sh\nexit 9\n", "failed-preflight")
    result = run_native(tmp_path, executable=hermes, preflight_argv=[str(failed)])
    assert result["status"] == "blocked"
    assert result["preflight_passed"] is False
    assert result["cleanup_verified"] is True
    assert not marker.exists()


def test_native_goal_claim_conflict_blocks_before_preflight(tmp_path):
    workspace = tmp_path / "workspace"
    corpus = workspace / "Doc Corpus"
    workspace.mkdir()
    corpus.mkdir()
    claim_db = corpus / "dedupe" / "live-trial-claims.sqlite"
    claim_db.parent.mkdir()
    store = launcher.RunClaimStore(claim_db)
    fp = launcher.scope_fingerprint("widget", "1.0")
    store.claim("default:Liv:native-goal", "widget", "1.0", fp,
                "other-owner", "existing-run", 9999999999, 300)
    marker = tmp_path / "preflight-started"
    preflight = executable(tmp_path, f"#!/bin/sh\ntouch '{marker}'\n", "preflight")
    result = launcher.run_native_goal(
        run_dir=corpus / "reports/runs/new-run", workspace=workspace,
        resume_session="paused", timeout=3, executable=str(executable(tmp_path)),
        verifier_argv=[str(verifier(tmp_path))], preflight_argv=[str(preflight)],
        claim_store=store, claim_namespace="default:Liv:native-goal",
        claim_product="widget", claim_version="1.0", claim_fingerprint=fp,
        claim_owner="new-owner", claim_run_id="new-run",
    )
    assert result["status"] == "attach-existing"
    assert result["existing_run_id"] == "existing-run"
    assert not marker.exists()


def test_native_goal_signal_interruption_releases_claim(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    corpus = workspace / "Doc Corpus"
    claim_db = corpus / "dedupe" / "claims.sqlite"
    workspace.mkdir()
    claim_db.parent.mkdir(parents=True)
    store = launcher.RunClaimStore(claim_db)
    fp = launcher.scope_fingerprint("widget", "1.0")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher.pty, "openpty", interrupted)
    with pytest.raises(KeyboardInterrupt):
        launcher.run_native_goal(
            run_dir=corpus / "reports/runs/run", workspace=workspace,
            resume_session="paused", timeout=3, executable=str(executable(tmp_path)),
            verifier_argv=[str(verifier(tmp_path))], claim_store=store,
            claim_namespace="default:Liv:native-goal", claim_product="widget",
            claim_version="1.0", claim_fingerprint=fp, claim_owner="owner",
            claim_run_id="run",
        )
    with store._connect() as db:
        assert db.execute("select count(*) from run_claims").fetchone()[0] == 0
