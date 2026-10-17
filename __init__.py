"""Librarian plugin — portable package entrypoint.

Librarian is a standalone Hermes *profile* (the launcher spawns
`hermes --profile librarian chat` as a subprocess), not an in-process
toolset. So register(ctx) here does NOT register tools; it surfaces the
installer/provisioner and lets Hermes load the bundled caller skill.
Actual behavior lives in the profile + pkg/librarian code.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def provision(target_profile_dir: str | None = None, dry_run: bool = False) -> dict:
    """Place host tools and provision the librarian profile.

    Steps (idempotent):
      1. copy profile assets (SOUL.md, OPERATING_PROTOCOL.md, config.yaml)
         into the target profile dir (default ~/.hermes/profiles/librarian).
      2. place the pkg/librarian package on the profile's python path.
      3. report what was written / would be written.
    Returns a JSON-able result dict.
    """
    root = _repo_root()
    profile_src = root / "pkg" / "profile"
    target = Path(target_profile_dir).expanduser() if target_profile_dir else (
        Path.home() / ".hermes" / "profiles" / "librarian"
    )
    planned, written = [], []
    for name in ("SOUL.md", "OPERATING_PROTOCOL.md", "config.yaml"):
        src = profile_src / name
        dst = target / name
        planned.append({"from": str(src), "to": str(dst)})
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
            if src.exists():
                dst.write_bytes(src.read_bytes())
                written.append(str(dst))
    return {"dry_run": dry_run, "profile_dir": str(target), "planned": planned, "written": written}


def register(ctx) -> None:  # Hermes plugin entrypoint
    """Register the librarian-provision tool + the caller skill."""
    try:
        ctx.register_tool(
            name="librarian_provision",
            toolset="librarian",
            schema={
                "name": "librarian_provision",
                "description": "Provision the librarian Hermes profile (place SOUL/OPERATING_PROTOCOL/config) and report placement. Idempotent; pass dry_run=true to preview.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_profile_dir": {"type": "string"},
                        "dry_run": {"type": "boolean", "default": False},
                    },
                },
            },
            handler=lambda args, **kw: json.dumps(
                provision(args.get("target_profile_dir"), bool(args.get("dry_run", False)))
            ),
        )
    except Exception as e:  # plugin load must never break host
        sys.stderr.write(f"[librarian] tool registration skipped: {e}\n")
