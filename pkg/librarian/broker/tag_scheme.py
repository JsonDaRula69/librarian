"""Hindsight tag scheme v2 registries, validation, and document tag derivation.

Governing document: ``hindsight-tuning/TAG-SCHEME.md`` (proposed 2026-10-02).
This module is the machine-readable half of that scheme: the registries, the
rules R1-R8, and the derivation used by the Librarian ingest path.  The CLI at
``tools/tag-lint.py`` is a thin wrapper so other writers (legacy ingest scripts,
the Hermes plugin) can validate without importing broker code.

Two tag jobs, kept apart on purpose:

* **Dimensions** (``corpus:``, ``doctype:``) partition memory for consolidation.
  They are scope-safe, single-valued, and mandatory on corpus documents (R2).
* **Attributes** (everything else) are retrieval filters only.  They may repeat
  and may be broad, because the scope config must never key on them (R3).

Values are flat: recall filters take no wildcards, so a tag value carries no
glob and no extra colon (R4/§8).
"""
from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

# --- registries (§2) -------------------------------------------------------

DIMENSION_TAGS = ("corpus", "doctype")
ATTRIBUTE_TAGS = ("provider", "origin", "repo", "commit", "version", "status",
                  "lang", "format", "project", "session", "parent", "mind", "about")
REGISTERED_TAGS = frozenset(DIMENSION_TAGS + ATTRIBUTE_TAGS)

DOCTYPE_VALUES = frozenset({"documentation", "reference", "api", "cli", "guide",
                            "index", "metadata", "spec"})
STATUS_VALUES = frozenset({"current", "superseded", "archived"})
FORMAT_VALUES = frozenset({"llms-txt", "llms-full", "agents-md", "md", "rst",
                           "html", "json"})
ORIGIN_VALUES = frozenset({"in-house", "vendor", "community", "upstream"})

# R4: target, not a system limit.  Justified by scope-identity multiplication.
MAX_TAGS_PER_DOCUMENT = 6

# Families each author is allowed to write (§R7).
AUTHOR_FAMILIES = {
    "librarian": frozenset(DIMENSION_TAGS + ("provider", "origin", "repo", "commit",
                                             "version", "status", "lang", "format",
                                             "project")),
    "plugin": frozenset({"session", "parent", "project"}),
    "identity": frozenset({"mind", "about", "project"}),
}

# R5: acquisition archaeology never belongs in memory; manifests hold it.
_ARCHAEOLOGY_TAGS = frozenset({
    "documentation", "first-party", "official", "official-documentation",
    "complete-corpus", "full-corpus-backfill", "official-openai",
    "openai-developers-codex", "github-upstream", "documentation-remediation",
})
_ARCHAEOLOGY_PATTERNS = (
    re.compile(r"^run[:-]"),
    re.compile(r"remediation[-_]?\d{6,8}"),
    re.compile(r"\bbackfill\b"),
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
)

_TAG_SHAPE = re.compile(r"^([a-z][a-z0-9_-]*):(.+)$")
_LANG_SHAPE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")
_COMMIT_SHAPE = re.compile(r"^[0-9a-f]{12}$")
_HEX_SHAPE = re.compile(r"^[0-9a-f]{7,40}$")
_SLUG_SHAPE = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
_FLAT_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def flat_value(value: str, *, fallback: str = "unknown") -> str:
    """Keep an already-flat value (versions keep their dots); slugify otherwise."""
    if isinstance(value, str) and _FLAT_VALUE.match(value.strip()):
        return value.strip()
    return slugify(value, fallback=fallback)


@dataclass(frozen=True)
class Finding:
    """One validation result.  ``severity`` is 'error' or 'warn'."""

    tag: str | None
    severity: str
    rule: str
    reason: str

    def as_dict(self) -> dict:
        return {"tag": self.tag, "severity": self.severity, "rule": self.rule,
                "reason": self.reason}


def _split(tag: str) -> tuple[str, str] | None:
    match = _TAG_SHAPE.match(tag) if isinstance(tag, str) else None
    return (match.group(1), match.group(2)) if match else None


def slugify(value: str, *, fallback: str = "unknown") -> str:
    """Flat, glob-free, colon-free slug for dimension values."""
    if not isinstance(value, str):
        return fallback
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or fallback


def archaeology_reason(tag: str) -> str | None:
    """Return why a tag is acquisition archaeology, or None (R5)."""
    parts = _split(tag)
    namespace, value = parts if parts else (None, None)
    bare = tag.lower()
    if bare in _ARCHAEOLOGY_TAGS:
        return f"'{tag}' is acquisition archaeology; manifests hold that history"
    if namespace == "documentation" and value and value.startswith("remediation"):
        return f"'{tag}' is a remediation batch label, not a document class"
    for pattern in _ARCHAEOLOGY_PATTERNS:
        if pattern.search(bare):
            return f"'{tag}' matches archaeology pattern {pattern.pattern!r}"
    return None


def validate(tags: Sequence[str], *, profile: str = "corpus", author: str | None = None,
             strict: bool = False) -> list[Finding]:
    """Validate one tag set.  Returns findings; callers decide on failing.

    ``profile`` is 'corpus' (dimensions required, R2), 'identity' (mind/about
    only) or 'session' (plugin session memories).  ``strict`` promotes the
    >MAX_TAGS warning to an error.
    """
    findings: list[Finding] = []
    if isinstance(tags, (str, bytes)) or not isinstance(tags, Iterable):
        return [Finding(None, "error", "shape", "tags must be a sequence of strings")]

    seen = list(tags)
    by_namespace: dict[str, list[str]] = {}
    for tag in seen:
        if not isinstance(tag, str) or not tag:
            findings.append(Finding(str(tag), "error", "R1", "tag must be a non-empty string"))
            continue
        # R5 first: for archaeology the migration mapping is the actionable fact,
        # and it would otherwise be masked by the R1 namespace error.
        reason = archaeology_reason(tag)
        if reason:
            findings.append(Finding(tag, "error", "R5", reason))
            continue
        parts = _split(tag)
        if parts is None:
            findings.append(Finding(tag, "error", "R1", "tag must be namespaced 'ns:value'"))
            continue
        namespace, value = parts
        if namespace not in REGISTERED_TAGS:
            findings.append(Finding(tag, "error", "R1", f"unregistered namespace '{namespace}'"))
            continue
        by_namespace.setdefault(namespace, []).append(value)
        if ":" in value:
            findings.append(Finding(tag, "warn", "R4", "value carries a colon; scope values stay flat"))
        if any(ch in value for ch in "*?[]"):
            findings.append(Finding(tag, "warn", "R4", "value carries a glob; filters take no wildcards"))
        if value.strip() != value or not value.strip():
            findings.append(Finding(tag, "error", "R4", "empty or padded value"))

        if namespace == "doctype" and value not in DOCTYPE_VALUES:
            findings.append(Finding(tag, "error", "R2", f"doctype '{value}' is not registered"))
        elif namespace == "status" and value not in STATUS_VALUES:
            findings.append(Finding(tag, "error", "R2", f"status '{value}' is not registered"))
        elif namespace == "format" and value not in FORMAT_VALUES:
            findings.append(Finding(tag, "error", "R2", f"format '{value}' is not registered"))
        elif namespace == "origin" and value not in ORIGIN_VALUES:
            findings.append(Finding(tag, "error", "R2", f"origin '{value}' is not registered"))
        elif namespace == "lang" and not _LANG_SHAPE.match(value):
            findings.append(Finding(tag, "warn", "R2", "lang should be an ISO code such as 'en'"))
        elif namespace == "commit" and not _COMMIT_SHAPE.match(value):
            if _HEX_SHAPE.match(value):
                findings.append(Finding(tag, "warn", "R2", "commit should be 12 hex characters"))
            else:
                findings.append(Finding(tag, "error", "R2", "commit must be hex"))
        elif namespace == "repo" and not re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", value):
            findings.append(Finding(tag, "warn", "R2", "repo should look like 'org/name'"))
        elif namespace == "about" and not _looks_like_reference(value):
            findings.append(Finding(tag, "warn", "R8", "about: should reference a memory or document id"))

    for dimension in DIMENSION_TAGS:
        values = by_namespace.get(dimension, [])
        if len(values) > 1:
            findings.append(Finding(f"{dimension}:*", "error", "R4",
                                    f"dimension '{dimension}' is single-valued but has {len(values)} values"))
        if profile == "corpus" and not values:
            findings.append(Finding(None, "error", "R2",
                                    f"corpus documents require exactly one '{dimension}:' tag"))

    if profile == "corpus":
        for namespace, values in by_namespace.items():
            if namespace in DIMENSION_TAGS:
                continue
            if namespace in ("session", "parent", "mind"):
                findings.append(Finding(f"{namespace}:{values[0]}", "error", "R7",
                                        "reserved for the plugin or identity sessions"))
    if profile == "identity" and not (by_namespace.get("mind") or by_namespace.get("about")):
        findings.append(Finding(None, "error", "R7", "identity memories carry mind: or about:"))
    if profile == "session" and not by_namespace.get("session"):
        findings.append(Finding(None, "error", "R7", "plugin session memories carry session:"))

    if author is not None:
        allowed = AUTHOR_FAMILIES.get(author)
        if allowed is None:
            findings.append(Finding(None, "error", "R7", f"unknown author '{author}'"))
        else:
            for namespace in by_namespace:
                if namespace not in allowed:
                    findings.append(Finding(f"{namespace}:*", "error", "R7",
                                            f"author '{author}' may not write '{namespace}:'"))

    if len(seen) > MAX_TAGS_PER_DOCUMENT:
        findings.append(Finding(None, "error" if strict else "warn", "R4",
                                f"{len(seen)} tags exceeds the {MAX_TAGS_PER_DOCUMENT}-tag target"))
    return findings


def _looks_like_reference(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return bool(re.fullmatch(r"file_[0-9a-f-]{8,}", value)) or bool(_HEX_SHAPE.match(value))


def summarize_tag_sets(tag_sets: Mapping[str, Sequence[str]] | Sequence[Sequence[str]],
                       *, author: str = "librarian") -> dict:
    """Compliance summary for one submission (§6 coverage, at the writer's side).

    ``tag_sets`` is either artifact-id -> tags or a plain list of tag sets.
    Every set is validated, so a run can prove its own compliance without
    reading the bank back.
    """
    rows = [list(tags) for tags in (tag_sets.values() if isinstance(tag_sets, Mapping)
                                    else tag_sets)]
    histogram = Counter(tuple(sorted(tags)) for tags in rows)
    findings = [finding for tags in rows
                for finding in validate(tags, profile="corpus", author=author)]
    errors_found = [finding for finding in findings if finding.severity == "error"]
    return {
        "documents": len(rows),
        "budget": MAX_TAGS_PER_DOCUMENT,
        "max_tags": max((len(tags) for tags in rows), default=0),
        "sets": [{"tags": sorted(tags), "count": count} for tags, count in histogram.most_common()],
        "errors": [finding.as_dict() for finding in errors_found],
        "warnings": [finding.as_dict() for finding in findings if finding.severity == "warn"],
        "compliant": not errors_found,
    }


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [finding for finding in findings if finding.severity == "error"]


def warnings(findings: Iterable[Finding]) -> list[Finding]:
    return [finding for finding in findings if finding.severity == "warn"]


# --- derivation for the Librarian ingest path (§2, §R2-R5) -----------------

_GITHUB_HOSTS = ("github.com", "raw.githubusercontent.com", "codeload.github.com",
                 "api.github.com", "objects.githubusercontent.com")


def provider_for(url: str | None) -> str:
    """Who published the source, from its canonical URL host."""
    host = ""
    if isinstance(url, str):
        match = re.match(r"^[a-z]+://([^/]+)", url.strip().lower())
        host = (match.group(1) if match else "").split(":")[0]
    if host.endswith("github.com") or any(host == h for h in _GITHUB_HOSTS) or "githubusercontent" in host:
        return "github"
    return "first-party"


def format_for(path: str | None, url: str | None = None) -> str:
    """Artefact kind from the prepared filename, then the URL."""
    candidate = (path or url or "").rsplit("/", 1)[-1].lower()
    if candidate.endswith("llms-full.txt"):
        return "llms-full"
    if candidate.endswith("llms.txt"):
        return "llms-txt"
    if candidate.endswith("agents.md"):
        return "agents-md"
    for suffix, kind in ((".md", "md"), (".rst", "rst"), (".html", "html"),
                         (".json", "json"), (".txt", "md")):
        if candidate.endswith(suffix):
            return kind
    return "md"


def doctype_for(path: str | None, url: str | None = None) -> str:
    """Document class, conservatively: prose docs unless the artefact says otherwise."""
    candidate = ((path or "") + " " + (url or "")).lower()
    name = (path or url or "").rsplit("/", 1)[-1].lower()
    if "llms.txt" in name or "llms-full.txt" in name or "/docs-index/" in candidate:
        return "index"
    if name.endswith("openapi.json") or name.endswith("schema.json") or name.endswith("schema.yaml"):
        return "spec"
    if name in ("release.json", "checksums.txt") or name.endswith("sha256sums.txt") \
            or "checksums" in name or name.endswith(".sig"):
        return "metadata"
    if "api-reference" in candidate or "/api/" in candidate:
        return "api"
    if name.startswith("readme") or name == "guide.md" or "manual" in name:
        return "guide"
    return "documentation"


def repo_value(value: str) -> str:
    """`org/name` stays intact (the registry value is not a slug); else flattened."""
    candidate = value.strip() if isinstance(value, str) else ""
    if re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", candidate):
        return candidate
    return slugify(candidate, fallback="unknown")


def derive_documentation_tags(*, product: str, version: str, canonical_url: str | None = None,
                              path: str | None = None, repository_ref: str | None = None,
                              status: str = "current") -> list[str]:
    """Compliant tag set for one submitted document.

    Order and budget: the two dimensions first, then attributes, capped at
    MAX_TAGS_PER_DOCUMENT.  ``repo:``/``commit:`` (pinned provenance) outrank
    ``format:`` when the budget is tight; ``format:`` is used for documents with
    no repo pin.  ``origin:`` is deliberately absent: publisher and product
    ownership cannot be inferred from a URL, and guessing would be worse than
    omitting a filter-only tag.
    """
    corpus = slugify(product)
    doctype = doctype_for(path, canonical_url)
    tags = [f"corpus:{corpus}", f"doctype:{doctype}",
            f"provider:{provider_for(canonical_url)}",
            f"version:{flat_value(version)}"]
    repo = commit = None
    if isinstance(repository_ref, str) and repository_ref:
        match = re.match(r"^(?P<repo>[^@\s]+)@(?P<sha>[0-9a-fA-F]{7,40})$", repository_ref.strip())
        if match:
            repo = f"repo:{repo_value(match.group('repo'))}"
            commit = f"commit:{match.group('sha')[:12].lower()}"
    if repo:
        tags.append(repo)
        if commit and len(tags) < MAX_TAGS_PER_DOCUMENT:
            tags.append(commit)
    if len(tags) < MAX_TAGS_PER_DOCUMENT:
        tags.append(f"format:{format_for(path, canonical_url)}")
    if status in STATUS_VALUES and status != "current" and len(tags) < MAX_TAGS_PER_DOCUMENT:
        # Only a non-current document needs the marker; tagging every doc
        # 'current' spends the budget on the default assumption.
        tags.append(f"status:{status}")
    return tags[:MAX_TAGS_PER_DOCUMENT]
