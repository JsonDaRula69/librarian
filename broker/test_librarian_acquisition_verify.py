import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location(
    "librarian_acquisition_verify", ROOT / "tools" / "librarian-acquisition-verify.py"
)
_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate)


def _fixture(tmp_path, *, tamper=False, missing_index=False, incomplete=False):
    corpus = tmp_path / "Doc Corpus"
    run = corpus / "reports" / "runs" / "r1"
    source = run / "sources" / "widget" / "1.2"
    source.mkdir(parents=True)
    payload = b"authoritative documentation\n"
    (source / "README.md").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema": 1, "run_id": "r1", "caller": "default", "bank": "Liv",
        "product": "widget", "version": "1.2", "source_id": "widget_1_2",
        "documentation_roots": ["reports/runs/r1/sources/widget/1.2"],
        "source_enumeration": {
            "status": "complete", "method": "fixture-index", "reason": "bounded fixture",
            "enumerated_count": 1, "enumerated_paths": [
                "reports/runs/r1/sources/widget/1.2/README.md"
            ],
            "required_surfaces": ["guides"],
            "surface_accounting": [{
                "surface": "guides", "status": "covered",
                "evidence_paths": ["reports/runs/r1/sources/widget/1.2/README.md"],
            }],
        },
        "files": [{
            "path": "reports/runs/r1/sources/widget/1.2/README.md",
            "sha256": digest, "canonical_url": "https://example.test/widget/README.md",
        }],
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    index = {
        "schema": 1, "run_id": "r1", "components": [{
            "product": "widget", "version": "1.2",
            "manifest_path": "reports/runs/r1/manifest.json",
        }],
        "completeness_accounting": {
            "status": "complete", "enumeration_methods": ["fixture-index"],
            "checked_surfaces": ["guides"],
            "alternative_indexes_checked": ["fixture navigation"],
            "versions_checked": ["1.2"],
            "dependency_boundaries_checked": ["none in fixture"],
            "challenge_path": "completeness-challenge.json",
        },
    }
    challenge = {"run_id": "r1", "status": "passed",
                 "component_manifest_paths": ["manifest.json"]}
    (run / "completeness-challenge.json").write_text(json.dumps(challenge), encoding="utf-8")
    if not missing_index:
        (run / "component-manifest-index.json").write_text(json.dumps(index), encoding="utf-8")
    gaps = {"schema": 1, "run_id": "r1", "status": "partial" if incomplete else "complete",
            "semantic_status": "partial" if incomplete else "complete",
            "gaps": ([{"boundary": "docs", "status": "partial", "evidence": "missing"}]
                     if incomplete else [])}
    (run / "gaps.json").write_text(json.dumps(gaps), encoding="utf-8")
    if tamper:
        (source / "README.md").write_bytes(b"tampered\n")
    return run, corpus


def test_complete_minimal_accounting_passes(tmp_path):
    run, corpus = _fixture(tmp_path)
    result = _gate.verify(run, corpus)
    assert result["ok"] is True
    assert result["components_checked"] == 1


@pytest.mark.parametrize("kind", ["tamper", "missing_index", "incomplete"])
def test_integrity_and_completion_fail_closed(tmp_path, kind):
    run, corpus = _fixture(tmp_path, **{kind: True})
    with pytest.raises(_gate.GateError) as exc:
        _gate.verify(run, corpus)
    assert (kind.replace("_", " ") in str(exc.value)
            or "gap" in str(exc.value)
            or "hash mismatch" in str(exc.value)
            or "component-manifest-index" in str(exc.value))


def test_current_run_has_complete_component_accounting():
    run = ROOT / "Doc Corpus/reports/runs/acquire-router-fix-20260908T010000Z"
    result = _gate.verify(run, ROOT / "Doc Corpus")
    assert result["ok"] is True
    assert result["components_checked"] == 11
    assert result["files_checked"] > 5_000


def test_component_manifest_path_accepts_run_relative_spelling(tmp_path):
    """Both the run-relative and corpus-relative spellings address the same file."""
    run, corpus = _fixture(tmp_path)
    index_path = run / "component-manifest-index.json"
    index = json.loads(index_path.read_text())
    assert index["components"][0]["manifest_path"] == "reports/runs/r1/manifest.json"
    assert _gate.verify(run, corpus)["ok"] is True
    index["components"][0]["manifest_path"] = "manifest.json"
    index_path.write_text(json.dumps(index))
    assert _gate.verify(run, corpus)["ok"] is True


def test_component_manifest_path_still_fails_closed_when_absent(tmp_path):
    run, corpus = _fixture(tmp_path)
    index_path = run / "component-manifest-index.json"
    index = json.loads(index_path.read_text())
    index["components"][0]["manifest_path"] = "manifest-absent.json"
    index_path.write_text(json.dumps(index))
    with pytest.raises(_gate.GateError):
        _gate.verify(run, corpus)
    index["components"][0]["manifest_path"] = "../outside/manifest.json"
    index_path.write_text(json.dumps(index))
    with pytest.raises(_gate.GateError):
        _gate.verify(run, corpus)


def test_relative_file_rejects_parent_traversal(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    with pytest.raises(_gate.GateError, match="canonical relative path"):
        _gate._relative_file(corpus, "../outside.txt", "fixture")


def test_verify_rejects_symlinked_run_ancestor(tmp_path):
    real_run, corpus = _fixture(tmp_path / "real")
    linked_corpus = tmp_path / "linked" / "Doc Corpus"
    linked_corpus.mkdir(parents=True)
    (linked_corpus / "reports").symlink_to(real_run.parents[1] / "reports", target_is_directory=True)
    linked_run = linked_corpus / "reports" / "runs" / "r1"
    with pytest.raises(_gate.GateError, match="symlink"):
        _gate.verify(linked_run, linked_corpus)


@pytest.mark.parametrize("name", ["component-manifest-index.json", "gaps.json"])
def test_verify_rejects_symlinked_control_evidence(tmp_path, name):
    run, corpus = _fixture(tmp_path)
    target = tmp_path / name
    target.write_bytes((run / name).read_bytes())
    (run / name).unlink()
    (run / name).symlink_to(target)
    with pytest.raises(_gate.GateError, match="symlink"):
        _gate.verify(run, corpus)


def test_verify_rejects_symlinked_corpus_ancestor(tmp_path):
    real_run, real_corpus = _fixture(tmp_path / "real")
    link = tmp_path / "corpus-parent"
    link.symlink_to(real_corpus.parent, target_is_directory=True)
    corpus = link / real_corpus.name
    run = corpus / real_run.relative_to(real_corpus)
    with pytest.raises(_gate.GateError, match="corpus-root.*symlink"):
        _gate.verify(run, corpus)


def test_json_read_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "oversized.json"
    path.write_text(json.dumps({"padding": "x" * 100}))
    monkeypatch.setattr(_gate, "MAX_JSON_BYTES", 32)
    with pytest.raises(_gate.GateError, match="JSON evidence exceeds bound"):
        _gate._read(path)


def test_manifest_file_must_be_under_documentation_root(tmp_path):
    run, corpus = _fixture(tmp_path)
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    other = run / "sources" / "other"
    other.mkdir()
    manifest["documentation_roots"] = ["reports/runs/r1/sources/other"]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(_gate.GateError, match="documentation_roots"):
        _gate.verify(run, corpus)


def test_total_byte_limit_is_run_wide(tmp_path, monkeypatch):
    run, corpus = _fixture(tmp_path)
    manifest = json.loads((run / "manifest.json").read_text())
    second = run / "manifest-2.json"
    manifest.update(product="widget-extra", source_id="widget_extra")
    second.write_text(json.dumps(manifest))
    index_path = run / "component-manifest-index.json"
    index = json.loads(index_path.read_text())
    index["components"].append({
        "product": "widget-extra", "version": "1.2",
        "manifest_path": "reports/runs/r1/manifest-2.json",
    })
    index_path.write_text(json.dumps(index))
    monkeypatch.setattr(_gate, "MAX_TOTAL_BYTES", 40)
    with pytest.raises(_gate.GateError, match="run bytes exceed bound"):
        _gate.verify(run, corpus)


def test_total_file_limit_is_run_wide(tmp_path, monkeypatch):
    run, corpus = _fixture(tmp_path)
    manifest = json.loads((run / "manifest.json").read_text())
    source = run / "sources" / "widget-extra" / "1.2"
    source.mkdir(parents=True)
    payload = b"second document\n"
    second_path = source / "README.md"
    second_path.write_bytes(payload)
    relative = "reports/runs/r1/sources/widget-extra/1.2/README.md"
    manifest.update(
        product="widget-extra", source_id="widget_extra",
        documentation_roots=["reports/runs/r1/sources/widget-extra/1.2"],
        files=[{"path": relative, "sha256": hashlib.sha256(payload).hexdigest(),
                "canonical_url": "https://example.test/widget-extra/README.md"}],
    )
    manifest["source_enumeration"]["enumerated_paths"] = [relative]
    manifest["source_enumeration"]["surface_accounting"][0]["evidence_paths"] = [relative]
    (run / "manifest-2.json").write_text(json.dumps(manifest))
    index_path = run / "component-manifest-index.json"
    index = json.loads(index_path.read_text())
    index["components"].append({"product": "widget-extra", "version": "1.2",
                                "manifest_path": "reports/runs/r1/manifest-2.json"})
    index_path.write_text(json.dumps(index))
    monkeypatch.setattr(_gate, "MAX_TOTAL_FILES", 1)
    with pytest.raises(_gate.GateError, match="run files exceed bound"):
        _gate.verify(run, corpus)


def test_required_surface_needs_evidence_backed_accounting(tmp_path):
    run, corpus = _fixture(tmp_path)
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source_enumeration"].pop("surface_accounting")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(_gate.GateError, match="surface accounting"):
        _gate.verify(run, corpus)


@pytest.mark.parametrize("url", ["not-a-url", "file:///etc/passwd", "https://user:pass@example.test/docs"])
def test_source_url_must_be_safe_http_identity(tmp_path, url):
    run, corpus = _fixture(tmp_path)
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["canonical_url"] = url
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(_gate.GateError, match="source identity URL"):
        _gate.verify(run, corpus)
