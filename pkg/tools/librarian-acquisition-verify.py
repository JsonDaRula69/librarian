#!/usr/bin/env python3
"""Read-only structural evidence gate for a normal Librarian acquisition run.

This checks manifest/index accounting and bytes; it cannot establish semantic
completeness. It never fetches, calls a model, or writes to the corpus.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    try:
        return Path.home() / ".hermes"
    except (RuntimeError, OSError):
        return Path(".hermes")


WORKSPACE = _hermes_home().parent / "Liv-Home"
DEFAULT_CORPUS = WORKSPACE / "Doc Corpus"
DEFAULT_RUN = DEFAULT_CORPUS / "reports/runs/acquire-router-fix-20260908T010000Z"
MAX_COMPONENTS = 256
MAX_FILES_PER_MANIFEST = 10_000
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_FILES = 50_000
MAX_JSON_BYTES = 16 * 1024 * 1024


class GateError(Exception):
    pass


def _read(path: Path) -> Any:
    try:
        if path.is_symlink():
            raise GateError(f"JSON evidence must not be a symlink: {path}")
        if path.stat().st_size > MAX_JSON_BYTES:
            raise GateError(f"JSON evidence exceeds bound: {path}")
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError as exc:
        raise GateError(f"missing evidence: {path.name}: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"invalid evidence {path}: {exc}") from exc


def _obj(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateError(f"{label} must be a JSON object")
    return value


def _relative_file(root: Path, raw: Any, label: str) -> Path:
    relative = Path(raw) if isinstance(raw, str) else Path()
    if (not isinstance(raw, str) or not raw or relative.is_absolute()
            or any(part in ("", ".", "..") for part in relative.parts)):
        raise GateError(f"{label} must be a canonical relative path")
    path = root / raw
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise GateError(f"{label} escapes corpus: {raw}") from exc
    probe = root
    for part in Path(raw).parts:
        probe /= part
        if probe.is_symlink():
            raise GateError(f"{label} uses symlink: {raw}")
    if not path.is_file() or path.is_symlink():
        raise GateError(f"missing evidence file: {raw}")
    return path


def _relative_directory(root: Path, raw: Any, label: str) -> Path:
    relative = Path(raw) if isinstance(raw, str) else Path()
    if (not isinstance(raw, str) or not raw or relative.is_absolute()
            or any(part in ("", ".", "..") for part in relative.parts)):
        raise GateError(f"{label} must be a canonical relative path")
    path = root / relative
    probe = root
    for part in relative.parts:
        probe /= part
        if probe.is_symlink():
            raise GateError(f"{label} uses symlink: {raw}")
    if not path.is_dir():
        raise GateError(f"missing evidence directory: {raw}")
    return path


def _safe_source_url(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise GateError(f"missing source identity URL: {label}")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise GateError(f"invalid source identity URL: {label}")
    return raw


def _has_symlink_ancestor(path: Path) -> bool:
    absolute = path.absolute()
    probe = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        probe /= part
        if probe.is_symlink():
            return True
    return False


def _resolve_component_manifest(corpus: Path, run: Path, raw: Any, label: str) -> Path:
    """Resolve a component manifest path against the run dir, then the corpus.

    The component index lives in the run directory and the completeness challenge
    names its components run-relatively, while a corpus-wide index may list paths
    from the corpus root. Both spellings address the same evidenced file, so
    accept either and still fail closed when neither exists.
    """
    relative = Path(raw) if isinstance(raw, str) else Path()
    if (not isinstance(raw, str) or not raw or relative.is_absolute()
            or any(part in ("", ".", "..") for part in relative.parts)):
        raise GateError(f"{label} must be a canonical relative path")
    try:
        return _relative_file(run, raw, label)
    except GateError:
        return _relative_file(corpus, raw, label)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(manifest: dict[str, Any], index_row: dict[str, Any], run_id: str) -> None:
    for key in ("run_id", "caller", "bank", "product", "version"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise GateError(f"manifest missing identity: {key}")
    if manifest["run_id"] != run_id:
        raise GateError("manifest run_id does not match index/run")
    if manifest["caller"] != "default" or manifest["bank"] != "Liv":
        raise GateError("manifest caller/bank is not bound to default/Liv")
    for key in ("product", "version"):
        if index_row.get(key) != manifest[key]:
            raise GateError(f"manifest/index identity mismatch: {key}")


def _verify_manifest(path: Path, corpus: Path, row: dict[str, Any], run_id: str) -> tuple[int, int]:
    manifest = _obj(_read(path), f"manifest {path}")
    _identity(manifest, row, run_id)
    roots = manifest.get("documentation_roots")
    if not isinstance(roots, list) or not roots or any(not isinstance(x, str) or not x for x in roots):
        raise GateError(f"manifest {path} missing documentation_roots")
    root_paths = [_relative_directory(corpus, raw, f"documentation_roots in {path}") for raw in roots]

    # A status flag is not source enumeration evidence. Require the enumerated
    # paths and reconcile them to the actual manifest rows.
    enumeration = manifest.get("source_enumeration")
    if enumeration is None:
        raise GateError(f"missing evidence: source enumeration not recorded in {path}")
    enumeration = _obj(enumeration, f"source_enumeration in {path}")
    status = enumeration.get("status")
    reason = enumeration.get("reason")
    enumerated = enumeration.get("enumerated_paths")
    surfaces = enumeration.get("required_surfaces")
    surface_accounting = enumeration.get("surface_accounting")
    if status != "complete" or not isinstance(reason, str) or not reason.strip():
        raise GateError(f"missing evidence: source enumeration is not justified in {path}")
    if (not isinstance(enumerated, list) or not enumerated or
            any(not isinstance(x, str) for x in enumerated)):
        raise GateError(f"missing evidence: source enumeration paths in {path}")
    if not isinstance(surfaces, list) or not surfaces or any(not isinstance(x, str) or not x for x in surfaces):
        raise GateError(f"missing evidence: required documentation surfaces in {path}")
    if not isinstance(surface_accounting, list) or len(surface_accounting) != len(surfaces):
        raise GateError(f"missing evidence: surface accounting in {path}")
    accounted_surfaces: set[str] = set()
    surface_paths: set[str] = set()
    for number, value in enumerate(surface_accounting, 1):
        account = _obj(value, f"surface accounting {number} in {path}")
        surface = account.get("surface")
        evidence_paths = account.get("evidence_paths")
        if (not isinstance(surface, str) or surface not in surfaces or surface in accounted_surfaces
                or account.get("status") not in {"covered", "unavailable", "not-applicable"}
                or not isinstance(evidence_paths, list)
                or any(not isinstance(item, str) or not item for item in evidence_paths)):
            raise GateError(f"invalid surface accounting in {path}")
        if account["status"] == "covered" and not evidence_paths:
            raise GateError(f"covered surface lacks evidence paths in {path}")
        accounted_surfaces.add(surface)
        surface_paths.update(evidence_paths)
    if accounted_surfaces != set(surfaces):
        raise GateError(f"surface accounting does not match required surfaces in {path}")

    rows = manifest.get("files", manifest.get("artifacts"))
    if not isinstance(rows, list) or not rows or len(rows) > MAX_FILES_PER_MANIFEST:
        raise GateError(f"manifest files missing or exceed bound: {path}")
    paths: list[str] = []
    total = 0
    for number, item in enumerate(rows, 1):
        record = _obj(item, f"manifest file {number}")
        raw_path = record.get("path", record.get("file"))
        expected = record.get("sha256", record.get("source_sha256"))
        if (not isinstance(raw_path, str) or not isinstance(expected, str) or len(expected) != 64
                or any(c not in "0123456789abcdef" for c in expected.lower())):
            raise GateError(f"invalid sha256 in {path} row {number}")
        actual_path = _relative_file(corpus, raw_path, f"manifest file {number}")
        if not any(actual_path.is_relative_to(root_path) for root_path in root_paths):
            raise GateError(f"manifest file is outside documentation_roots: {raw_path}")
        size = actual_path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise GateError(f"manifest bytes exceed bound: {path}")
        if _sha256(actual_path) != expected.lower():
            raise GateError(f"hash mismatch: {raw_path}")
        _safe_source_url(record.get("canonical_url", record.get("source_url", record.get("url"))), raw_path)
        paths.append(raw_path)
    if len(set(paths)) != len(paths) or set(enumerated) != set(paths) or len(enumerated) != len(paths):
        raise GateError(f"source enumeration does not account for manifest files: {path}")
    if not surface_paths.issubset(set(paths)):
        raise GateError(f"surface accounting references unmanifested evidence in {path}")
    return len(rows), total


def _verify_run_semantics(run: Path, index: dict[str, Any], gaps: dict[str, Any], paths: set[str]) -> None:
    accounting = _obj(index.get("completeness_accounting"), "component completeness accounting")
    methods = accounting.get("enumeration_methods")
    checked = accounting.get("checked_surfaces")
    if (accounting.get("status") != "complete" or not isinstance(methods, list) or not methods
            or any(not isinstance(value, str) or not value for value in methods)
            or not isinstance(checked, list) or not checked
            or any(not isinstance(value, str) or not value for value in checked)):
        raise GateError("missing evidence: run completeness accounting")
    for key in ("alternative_indexes_checked", "versions_checked", "dependency_boundaries_checked"):
        if not isinstance(accounting.get(key), list) or not accounting[key]:
            raise GateError(f"missing evidence: {key}")
    challenge_path = _relative_file(
        run, accounting.get("challenge_path"), "completeness challenge path")
    challenge = _obj(_read(challenge_path), "completeness challenge")
    if challenge.get("status") != "passed" or challenge.get("run_id") != index.get("run_id"):
        raise GateError("completeness challenge has not passed")
    if set(challenge.get("component_manifest_paths", [])) != paths:
        raise GateError("completeness challenge does not account for component manifests")
    if gaps.get("semantic_status") != "complete":
        raise GateError("gaps evidence does not establish semantic completeness")


def verify(run_dir: str | Path = DEFAULT_RUN, corpus_root: str | Path = DEFAULT_CORPUS) -> dict[str, Any]:
    run = Path(run_dir).absolute()
    corpus = Path(corpus_root).absolute()
    if _has_symlink_ancestor(corpus) or not corpus.is_dir():
        raise GateError("corpus-root must be an existing non-symlink directory")
    if not run.is_relative_to(corpus) or run.is_symlink():
        raise GateError("run-dir must be a non-symlink descendant of Liv Doc Corpus")
    probe = corpus
    for part in run.relative_to(corpus).parts:
        probe /= part
        if probe.is_symlink():
            raise GateError("run-dir must not contain symlink ancestors")
    if not run.is_dir():
        raise GateError("run-dir must be an existing directory")
    run_id_path = run / "component-manifest-index.json"
    index = _obj(_read(run_id_path), "component-manifest-index")
    run_id = index.get("run_id")
    components = index.get("components")
    if not isinstance(components, list) or not components:
        raise GateError("component-manifest-index missing run_id/components")
    if not isinstance(run_id, str) or not run_id:
        raise GateError("component-manifest-index missing run_id")
    if len(components) > MAX_COMPONENTS:
        raise GateError("component-manifest-index exceeds component bound")
    gaps = _obj(_read(run / "gaps.json"), "gaps")
    if gaps.get("run_id") != run_id:
        raise GateError("gaps run_id does not match index")
    # A gap blocks only when it is genuinely unresolved. Resolved notes and
    # documented exclusions are recorded provenance, not outstanding work.
    blocking_kinds = {"unresolved", "concrete_failure", "blocked"}
    entries = gaps.get("gaps")
    blocking = []
    if isinstance(entries, list):
        for x in entries:
            if isinstance(x, dict) and (x.get("status") in blocking_kinds
                                        or x.get("kind") in blocking_kinds):
                blocking.append(x)
    if gaps.get("status") != "complete" and blocking:
        detail = "; ".join(str(x.get("boundary", x.get("id", x))) for x in blocking) or "see gaps.json"
        raise GateError(f"explicit acquisition gaps remain ({gaps.get('status')}): {detail}")
    seen: set[str] = set()
    manifest_paths: set[str] = set()
    total_files = total_bytes = 0
    for number, value in enumerate(components, 1):
        row = _obj(value, f"component {number}")
        raw_manifest = row.get("manifest_path")
        manifest_path = _resolve_component_manifest(corpus, run, raw_manifest, f"component {number} manifest_path")
        manifest_paths.add(str(manifest_path.relative_to(run)))
        key = str((row.get("product"), row.get("version"), manifest_path))
        if key in seen:
            raise GateError(f"duplicate component accounting: {key}")
        seen.add(key)
        count, size = _verify_manifest(manifest_path, corpus, row, run_id)
        total_files += count
        total_bytes += size
        if total_files > MAX_TOTAL_FILES:
            raise GateError("run files exceed bound")
        if total_bytes > MAX_TOTAL_BYTES:
            raise GateError("run bytes exceed bound")

    _verify_run_semantics(run, index, gaps, manifest_paths)

    return {"ok": True, "run_id": run_id, "components_checked": len(components), "files_checked": total_files, "bytes_checked": total_bytes,
            "note": "structural evidence only; semantic completeness and live ingestion approval are not established"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN))
    parser.add_argument("--corpus-root", default=str(DEFAULT_CORPUS))
    args = parser.parse_args(argv)
    try:
        print(json.dumps(verify(args.run_dir, args.corpus_root), sort_keys=True))
        return 0
    except GateError as exc:
        print(f"acquisition evidence gate: FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
