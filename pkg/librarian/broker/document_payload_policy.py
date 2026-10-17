"""Safety classification for acquired documentation payloads.

This module only describes an artifact; it never deletes, rewrites, or decodes it
for submission.  Callers retain the original bytes and provenance separately.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
from pathlib import Path
import re
from typing import Any


INGESTIBLE_TEXT = "ingestible-text"
ASSET_ONLY = "asset-only"
NEEDS_EXTRACTION = "needs-extraction"
UNRESOLVED = "unresolved"

_TEXT_EXTENSIONS = {
    ".md", ".markdown", ".rst", ".txt", ".text", ".html", ".htm",
    ".json", ".jsonl", ".yaml", ".yml", ".xml", ".xhtml", ".adoc",
    ".asc", ".csv", ".ini", ".cfg", ".conf", ".toml", ".schema",
}
_CODE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".java", ".go", ".rs",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".bash", ".fish", ".rb",
    ".php", ".swift", ".kt", ".kts", ".cs", ".sql", ".lua", ".pl",
}
_DOC_PARTS = {"doc", "docs", "documentation", "example", "examples", "sample", "samples"}


@dataclass(frozen=True)
class PayloadClassification:
    """Classification plus stable identity/provenance fields for an artifact."""

    classification: str
    reason: str
    sha256: str
    path: str | None = None
    mime_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    # Mapping-like convenience for callers that serialize policy decisions.
    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


def _read_payload(value: Any, path: str | Path | None) -> tuple[bytes, str | None]:
    if isinstance(value, (str, Path)) and path is None:
        path = str(value)
        data = Path(value).read_bytes()
    elif isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
    elif isinstance(value, dict):
        path = value.get("path", path)
        raw = value.get("bytes", value.get("data", value.get("content")))
        if isinstance(raw, (str, Path)) and value.get("path") is not None:
            data = Path(raw).read_bytes()
        elif isinstance(raw, str):
            data = raw.encode()
        elif isinstance(raw, (bytes, bytearray, memoryview)):
            data = bytes(raw)
        else:
            raise TypeError("artifact must contain bytes, data, content, or path")
    else:
        raise TypeError("artifact must be bytes, a path, or an artifact mapping")
    return data, str(path) if path is not None else None


def _mime_kind(mime: str | None) -> str | None:
    if not mime:
        return None
    mime = mime.lower().split(";", 1)[0].strip()
    if mime == "application/pdf" or "word" in mime or "officedocument" in mime or mime in {
        "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
    }:
        return NEEDS_EXTRACTION
    if mime.startswith(("image/", "audio/", "video/")) or mime in {
        "application/octet-stream", "application/x-executable", "application/x-sharedlib",
        "application/vnd.wasm",
    }:
        return ASSET_ONLY
    if mime.startswith("text/") or mime in {
        "application/json", "application/ld+json", "application/xml", "application/yaml",
        "text/html", "text/markdown",
    }:
        return INGESTIBLE_TEXT
    return None


def _magic_kind(data: bytes) -> tuple[str | None, str | None]:
    signatures = (
        (b"%PDF-", NEEDS_EXTRACTION, "pdf-requires-text-extraction"),
        (b"PK\x03\x04", NEEDS_EXTRACTION, "archive-or-office-container-requires-extraction"),
        (b"PK\x05\x06", NEEDS_EXTRACTION, "archive-requires-extraction"),
        (b"\x1f\x8b", NEEDS_EXTRACTION, "compressed-archive-requires-extraction"),
        (b"BZh", NEEDS_EXTRACTION, "compressed-archive-requires-extraction"),
        (b"7z\xbc\xaf\x27\x1c", NEEDS_EXTRACTION, "archive-requires-extraction"),
        (b"\x89PNG\r\n\x1a\n", ASSET_ONLY, "image-binary"),
        (b"\xff\xd8\xff", ASSET_ONLY, "image-binary"),
        (b"GIF8", ASSET_ONLY, "image-binary"),
        (b"RIFF", ASSET_ONLY, "image-or-audio-binary"),
        (b"\x7fELF", ASSET_ONLY, "elf-binary"),
        (b"MZ", ASSET_ONLY, "pe-or-dos-binary"),
        (b"\xca\xfe\xba\xbe", ASSET_ONLY, "compiled-object-binary"),
        (b"\xce\xfa\xed\xfe", ASSET_ONLY, "mach-o-binary"),
        (b"\xcf\xfa\xed\xfe", ASSET_ONLY, "mach-o-binary"),
        (b"\x00asm", ASSET_ONLY, "wasm-binary"),
    )
    for signature, kind, reason in signatures:
        if data.startswith(signature):
            return kind, reason
    return None, None


def _doc_boundary(path: str | None, evidence: Any) -> bool:
    if evidence is True:
        return True
    if isinstance(evidence, dict):
        return bool(evidence.get("documentation_boundary") or evidence.get("explicit"))
    if isinstance(evidence, (list, tuple, set)):
        return any(_doc_boundary(path, item) for item in evidence)
    if isinstance(evidence, str):
        return evidence.lower() in {"documentation", "docs", "examples", "explicit"}
    # A filename is not authority to cross the documentation trust boundary.
    return False


def classify_payload(
    artifact: Any,
    *,
    path: str | Path | None = None,
    mime_type: str | None = None,
    documentation_boundary: Any = None,
) -> PayloadClassification:
    """Classify bytes/path without turning unsafe bytes into base64 text.

    Supplied MIME is authoritative for recognized types, then magic bytes, then
    the extension.  Unknown binary/text and code outside an explicit docs
    boundary remain unresolved rather than being silently submitted.
    """
    data, path = _read_payload(artifact, path)
    if isinstance(artifact, dict):
        mime_type = artifact.get("mime_type", artifact.get("mime", mime_type))
    digest = hashlib.sha256(data).hexdigest()
    ext = Path(path).suffix.lower() if path else ""

    mime_kind = _mime_kind(mime_type)
    magic_kind, magic_reason = _magic_kind(data)
    # Validate text claims against the bytes first. MIME cannot override a
    # NUL or invalid UTF-8 payload.
    text_is_safe = True
    try:
        decoded = data.decode("utf-8")
        text_is_safe = "\x00" not in decoded
    except UnicodeDecodeError:
        text_is_safe = False
    # Recognized MIME wins over a misleading filename only after that check;
    # magic still wins over an unrecognized or generic acquisition MIME.
    if mime_kind is not None and mime_kind != INGESTIBLE_TEXT:
        classification, reason = mime_kind, "mime-type-indicates-" + mime_kind
    elif magic_kind is not None:
        classification, reason = magic_kind, magic_reason or "content-signature"
    elif mime_kind == INGESTIBLE_TEXT and text_is_safe:
        classification, reason = mime_kind, "mime-type-indicates-text"
    elif mime_kind == INGESTIBLE_TEXT:
        classification, reason = ASSET_ONLY, "binary-content-not-safe-as-text"
    else:
        if not text_is_safe:
            classification, reason = ASSET_ONLY, "binary-content-not-safe-as-text"
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                classification, reason = ASSET_ONLY, "binary-content-not-safe-as-text"
            else:
                if ext in _TEXT_EXTENSIONS:
                    classification, reason = INGESTIBLE_TEXT, "recognized-authored-text-format"
                elif ext in _CODE_EXTENSIONS:
                    if _doc_boundary(path, documentation_boundary):
                        classification, reason = INGESTIBLE_TEXT, "code-preserved-inside-documentation-boundary"
                    else:
                        classification, reason = UNRESOLVED, "code-outside-explicit-documentation-boundary"
                elif not ext and text.strip():
                    classification, reason = INGESTIBLE_TEXT, "extensionless-readable-text"
                elif text.strip():
                    classification, reason = UNRESOLVED, "unknown-text-format"
                else:
                    classification, reason = UNRESOLVED, "empty-or-unidentified-payload"
    return PayloadClassification(classification, reason, digest, path, mime_type)


# Short alias for adapters that call this a policy decision.
classify_artifact = classify_payload

__all__ = [
    "ASSET_ONLY", "INGESTIBLE_TEXT", "NEEDS_EXTRACTION", "UNRESOLVED",
    "PayloadClassification", "classify_artifact", "classify_payload",
]
