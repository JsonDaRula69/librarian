"""Tag scheme v2: registries, rules R1-R7, and document tag derivation."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from broker.tag_scheme import (MAX_TAGS_PER_DOCUMENT, archaeology_reason,
                               derive_documentation_tags, errors, slugify, validate,
                               warnings)

ROOT = Path(__file__).resolve().parents[1]
COMPLIANT = ["corpus:jq", "doctype:guide", "provider:github", "version:1.8.1",
             "repo:jqlang/jq", "commit:4ef89209a239"]


def test_compliant_document_set_passes():
    assert errors(validate(COMPLIANT, profile="corpus", author="librarian")) == []
    assert len(COMPLIANT) <= MAX_TAGS_PER_DOCUMENT


@pytest.mark.parametrize("tag,rule", [
    ("Docs:guide", "R1"),                    # unregistered namespace
    ("corpus", "R1"),                        # no value separator
    ("documentation", "R5"),                 # bare hub tag
    ("Documentation", "R5"),                 # same hub tag, any case
    ("first-party", "R5"),
    ("complete-corpus", "R5"),
    ("full-corpus-backfill", "R5"),
    ("documentation-remediation-20260826", "R5"),
    ("run-20260826T120000Z", "R5"),
    ("2026-08-26", "R5"),
])
def test_rejected_tags(tag, rule):
    findings = validate(["corpus:jq", "doctype:guide", tag], profile="corpus")
    assert any(f.rule == rule and f.tag == tag for f in errors(findings)), findings


def test_dimensions_are_required_single_valued_and_registered():
    assert any(f.rule == "R2" for f in errors(validate(["doctype:guide"], profile="corpus")))
    assert any(f.rule == "R2" for f in errors(validate(["corpus:jq"], profile="corpus")))
    doubled = validate(["corpus:jq", "corpus:ripgrep", "doctype:guide"], profile="corpus")
    assert any("single-valued" in f.reason for f in errors(doubled))
    assert any(f.rule == "R2" for f in errors(validate(["corpus:jq", "doctype:banana"])))


def test_tag_budget_warns_and_strict_fails():
    seven = COMPLIANT + ["format:md"]
    assert warnings(validate(seven, profile="corpus")) != []
    assert errors(validate(seven, profile="corpus")) == []
    assert errors(validate(seven, profile="corpus", strict=True)) != []


def test_author_families_are_enforced():
    assert any(f.rule == "R7" for f in errors(
        validate(COMPLIANT + ["session:abc"], profile="corpus", author="librarian")))
    assert errors(validate(["session:abc", "parent:def"], profile="session", author="plugin")) == []
    assert any(f.rule == "R7" for f in errors(
        validate(["session:abc", "corpus:jq"], profile="session", author="plugin")))
    assert errors(validate(["mind:fucks:7", "about:1f0c1f0c-1111-2222-3333-444455556666"],
                           profile="identity", author="identity")) == []
    assert any(f.rule == "R7" for f in errors(
        validate(["project:hindsight-tuning"], profile="identity", author="identity")))


def test_value_registries_and_flat_values():
    assert any(f.rule == "R2" for f in errors(validate(["corpus:jq", "doctype:guide", "status:stale"])))
    assert any(f.rule == "R2" for f in errors(validate(["corpus:jq", "doctype:guide", "format:pdf"])))
    glob = validate(["corpus:j*", "doctype:guide"], profile="corpus")
    assert any("glob" in f.reason for f in warnings(glob))
    colons = validate(["corpus:a:b", "doctype:guide"], profile="corpus")
    assert any("colon" in f.reason for f in warnings(colons))
    commit = validate(["corpus:jq", "doctype:guide", "commit:deadbeef"], profile="corpus")
    assert any("commit should be 12 hex" in f.reason for f in warnings(commit))
    assert any(f.rule == "R2" for f in errors(validate(["corpus:jq", "doctype:guide", "commit:xyz"])))


def test_archaeology_helper_is_specific():
    assert archaeology_reason("complete-corpus") is not None
    assert archaeology_reason("corpus:jq") is None
    assert archaeology_reason("doctype:documentation") is None


def test_slugify_flattens_values():
    assert slugify("PCRE2") == "pcre2"
    assert slugify("github-cli") == "github-cli"
    assert slugify("code-yeongyu/oh-my-openagent@ec3d5afa") == "code-yeongyu-oh-my-openagent-ec3d5afa"
    assert slugify("") == "unknown"


def test_repo_backed_document_derivation_uses_six_tags():
    tags = derive_documentation_tags(
        product="github-cli", version="2.93.0",
        canonical_url="https://raw.githubusercontent.com/cli/cli/v2.93.0/docs/README.md",
        path="sources/cli-2.93.0/docs/README.md",
        repository_ref="cli/cli@8f3369ade41f")
    assert tags == ["corpus:github-cli", "doctype:guide", "provider:github",
                    "version:2.93.0", "repo:cli/cli", "commit:8f3369ade41f"]
    assert len(tags) == MAX_TAGS_PER_DOCUMENT
    assert errors(validate(tags, profile="corpus", author="librarian")) == []


def test_web_document_derivation_falls_back_to_format():
    tags = derive_documentation_tags(
        product="infisical", version="0.162.15",
        canonical_url="https://infisical.com/docs/llms-full.txt", path="pages-md/llms-full.txt")
    assert tags == ["corpus:infisical", "doctype:index", "provider:first-party",
                    "version:0.162.15", "format:llms-full"]
    assert errors(validate(tags, profile="corpus", author="librarian")) == []


@pytest.mark.parametrize("path,expected", [
    ("pages-md/api-reference/overview/introduction.md", "api"),
    ("openapi.json", "spec"),
    ("release.json", "metadata"),
    ("docs/llms.txt", "index"),
    ("README.md", "guide"),
    ("CHANGELOG.md", "documentation"),
])
def test_doctype_classification(path, expected):
    tags = derive_documentation_tags(product="x", version="1", path=path)
    assert f"doctype:{expected}" in tags


def test_documentation_tags_from_controller_context_prefers_pinned_repo():
    from broker.controller_integrations import _repository_slug, documentation_tags
    assert _repository_slug("https://github.com/jqlang/jq@4ef89209a239c1aea328cf13c05a2807e5c146d1:docs/README.md") \
        == "jqlang/jq@4ef89209a239c1aea328cf13c05a2807e5c146d1"
    assert _repository_slug("https://raw.githubusercontent.com/cli/cli/v2.93.0/docs/README.md") is None
    tags = documentation_tags({
        "product": "ripgrep", "version": "14.1.0",
        "provenance": [{"source_url": "https://raw.githubusercontent.com/BurntSushi/ripgrep/14.1.0/GUIDE.md",
                        "path": "sources/ripgrep/14.1.0/GUIDE.md",
                        "repository_ref": "https://github.com/BurntSushi/ripgrep@0123456789abcdef: GUIDE.md"}]})
    assert tags[:2] == ["corpus:ripgrep", "doctype:guide"]
    assert "repo:BurntSushi/ripgrep" in tags and "commit:0123456789ab" in tags


def test_lint_cli_reports_pass_and_fail_clearly():
    ok = subprocess.run([sys.executable, str(ROOT / "tools/tag-lint.py"), "--tags",
                         ",".join(COMPLIANT), "--author", "librarian", "--json"],
                        capture_output=True, text=True, cwd=ROOT)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    payload = json.loads(ok.stdout)
    assert payload["ok"] is True and payload["findings"] == []

    bad = subprocess.run([sys.executable, str(ROOT / "tools/tag-lint.py"), "--tags",
                          "documentation,first-party", "--json"],
                         capture_output=True, text=True, cwd=ROOT)
    assert bad.returncode == 1
    assert {f["rule"] for f in json.loads(bad.stdout)["findings"]} == {"R2", "R5"}


def test_submission_summary_reports_compliance_and_histogram():
    from broker.tag_scheme import summarize_tag_sets

    summary = summarize_tag_sets({
        "a": ["corpus:jq", "doctype:guide", "version:1.8.1"],
        "b": ["corpus:jq", "doctype:guide", "version:1.8.1"],
        "c": ["documentation"]})
    assert summary["documents"] == 3
    assert summary["compliant"] is False
    assert summary["max_tags"] == 3 and summary["budget"] == MAX_TAGS_PER_DOCUMENT
    assert summary["sets"][0]["count"] == 2 and summary["sets"][0]["tags"][0] == "corpus:jq"
    assert any(finding["rule"] == "R5" for finding in summary["errors"])

    clean = summarize_tag_sets([["corpus:jq", "doctype:guide"], ["corpus:jq", "doctype:guide"]])
    assert clean["compliant"] is True and clean["errors"] == [] and clean["documents"] == 2
