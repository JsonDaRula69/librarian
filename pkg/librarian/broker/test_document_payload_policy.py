from __future__ import annotations

import hashlib
import zipfile
from io import BytesIO

from broker.document_payload_policy import (
    ASSET_ONLY,
    INGESTIBLE_TEXT,
    NEEDS_EXTRACTION,
    UNRESOLVED,
    classify_payload,
)


def test_binary_content_cannot_masquerade_as_markdown():
    result = classify_payload(b"\x7fELF" + b"not markdown", path="README.md")
    assert result.classification == ASSET_ONLY
    assert result.reason == "elf-binary"
    assert result.sha256 == hashlib.sha256(b"\x7fELF" + b"not markdown").hexdigest()
    assert result.path == "README.md"


def test_extensionless_abi_text_is_preserved():
    result = classify_payload(b"type Query { hello: String }\n", path="abi")
    assert result.classification == INGESTIBLE_TEXT
    assert result.reason == "extensionless-readable-text"


def test_schema_json_is_ingestible_text():
    result = classify_payload(
        b'{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        b'"type":"object"}',
        path="config.schema.json",
        mime_type="application/json",
    )
    assert result.classification == INGESTIBLE_TEXT
    assert result.mime_type == "application/json"


def test_archive_is_never_submitted_as_text():
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("README.md", "documentation")
    result = classify_payload(output.getvalue(), path="README.md")
    assert result.classification == NEEDS_EXTRACTION
    assert "archive" in result.reason


def test_pdf_requires_extraction_even_with_text_extension():
    result = classify_payload(b"%PDF-1.7\n", path="manual.txt")
    assert result.classification == NEEDS_EXTRACTION
    assert result.reason == "pdf-requires-text-extraction"


def test_office_mime_requires_extraction():
    result = classify_payload(
        b"PK\x03\x04container", path="guide.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert result.classification == NEEDS_EXTRACTION


def test_code_path_alone_is_not_a_documentation_boundary():
    result = classify_payload(
        b"def hello():\n    return 'docs'\n", path="examples/hello.py",
    )
    assert result.classification == UNRESOLVED


def test_code_inside_explicit_docs_boundary_is_preserved():
    result = classify_payload(
        b"def hello():\n    return 'docs'\n", path="examples/hello.py",
        documentation_boundary=True,
    )
    assert result.classification == INGESTIBLE_TEXT
    assert result.reason == "code-preserved-inside-documentation-boundary"


def test_code_outside_docs_boundary_is_unresolved():
    result = classify_payload(b"def hello():\n    return 1\n", path="src/hello.py")
    assert result.classification == UNRESOLVED
    assert result.reason == "code-outside-explicit-documentation-boundary"


def test_explicit_boundary_evidence_can_preserve_code_anywhere():
    result = classify_payload(
        b"fn main() {}", path="src/main.rs",
        documentation_boundary={"documentation_boundary": "reviewed-doc-example"},
    )
    assert result.classification == INGESTIBLE_TEXT
