#!/usr/bin/env python3
"""Safe, caller-bound import helper for the existing controller ingestion adapter.

This tool never acquires files or creates a second ledger.  A caller supplies a
manifest naming files already downloaded inside the Liv corpus.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from broker.acquisition_controller import AcquisitionController, BoundaryError, read_safe, sha
from broker.controller_integrations import ControllerIngestion, artifact_tag_sets
from broker.corpus_preflight import (BankDocument, BankReaderResult, CorpusKey,
                                     PreflightDisposition, PreflightResult, assess_reuse)
from broker.document_payload_policy import classify_payload
from broker.hindsight_inventory import HindsightInventoryReader
from broker.transport import HindsightHTTPTransport
from broker.tag_scheme import summarize_tag_sets

DEFAULT_WORKSPACE = Path("/home/jsondarula/Liv-Home")
DEFAULT_CORPUS = DEFAULT_WORKSPACE / "Doc Corpus"
DEFAULT_BANK = "Liv"
DEFAULT_CALLER = "default"
HERMES_ENV = Path("/home/jsondarula/.hermes/.env")
DEV_CORPUS = DEFAULT_WORKSPACE / "Doc Corpus" / "dev-hindsight"
DEV_ENV = DEFAULT_WORKSPACE / ".hermes" / "dev-hindsight.env"
DEV_ENDPOINT = "http://127.0.0.1:18888"
DEV_LEDGER = DEV_CORPUS / "dedupe" / "dev-ledger.json"


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BoundaryError("manifest must be a JSON object")
    return value


def _workspace(value: str | Path | None) -> Path:
    path = Path(DEFAULT_WORKSPACE if value is None else value).absolute()
    if path != Path(DEFAULT_WORKSPACE).absolute():
        raise BoundaryError("caller workspace is fixed to Liv workspace")
    return path


def _corpus_root(value: str | Path | None, manifest: dict[str, Any], workspace: Path,
                 *, dev_instance: bool = False) -> Path:
    """Allow only the deployment corpus or a lexical, non-symlink descendant."""
    if dev_instance:
        # The dev corpus is trusted configuration, not a manifest-controlled
        # destination.  Sources are copied there by import_manifest.
        requested = value if value is not None else DEV_CORPUS
        if Path(requested).absolute() != Path(DEV_CORPUS).absolute():
            raise BoundaryError("dev instance corpus is fixed")
        if Path(DEV_CORPUS).is_symlink():
            raise BoundaryError("symlink dev corpus is not trusted")
        source = manifest.get("corpus_root", DEFAULT_CORPUS)
        if Path(source).absolute() != Path(DEFAULT_CORPUS).absolute():
            raise BoundaryError("dev instance source corpus is fixed")
        return Path(DEV_CORPUS).absolute()
    raw = value if value is not None else manifest.get("corpus_root", DEFAULT_CORPUS)
    root = Path(raw).absolute()
    base = Path(DEFAULT_CORPUS).absolute()
    if root != base and not root.is_relative_to(base):
        raise BoundaryError("corpus must be DEFAULT_CORPUS or a descendant")
    current = base
    if root != base:
        for part in root.relative_to(base).parts:
            current = current / part
            if current.is_symlink():
                raise BoundaryError("symlink corpus path is not trusted")
    return root


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows = manifest.get("files", manifest.get("artifacts"))
    if not isinstance(rows, list) or not rows:
        raise BoundaryError("manifest requires non-empty files")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise BoundaryError("manifest file record must be an object")
        path = row.get("path", row.get("file"))
        digest = row.get("sha256", row.get("source_sha256"))
        url = row.get("canonical_url", row.get("source_url", row.get("url")))
        if (not isinstance(path, str) or not isinstance(digest, str)
                or not isinstance(url, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest.lower())
                or not url.startswith(("https://", "http://"))):
            raise BoundaryError("each file needs path, sha256, and canonical_url")
        result.append(dict(row, path=path, sha256=digest.lower(), canonical_url=url))
    return result


def _component_run_id(parent_run_id: str, source_id: str, product: str, version: str) -> str:
    """Bind controller state to one component while retaining parent lineage."""
    identity = json.dumps({"product": product, "source_id": source_id,
                           "version": version}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{parent_run_id}--component-{digest}"


def import_manifest(manifest_path: str | Path, *, workspace: str | Path | None = None,
                    corpus: str | Path | None = None, dev_instance: bool = False,
                    run_id: str | None = None,
                    _controller_run_id: str | None = None) -> tuple[AcquisitionController, dict[str, Any]]:
    """Create controller evidence from verified local files, never from claims."""
    workspace_path = _workspace(workspace)
    manifest = _json(Path(manifest_path).resolve())
    corpus_root = _corpus_root(corpus, manifest, workspace_path, dev_instance=dev_instance)
    source_root = Path(manifest.get("corpus_root", DEFAULT_CORPUS)).absolute()
    run_id = run_id or manifest.get("run_id")
    bank = manifest.get("bank", DEFAULT_BANK)
    caller = manifest.get("caller", DEFAULT_CALLER)
    product = manifest.get("product", manifest.get("product_id"))
    version = manifest.get("version")
    if not all(isinstance(x, str) and x for x in (run_id, bank, product, version, caller)):
        raise BoundaryError("manifest requires run_id, product, version, and bank")
    assert isinstance(run_id, str)
    if bank != DEFAULT_BANK:
        raise BoundaryError("bank is fixed to Liv")
    if caller != DEFAULT_CALLER:
        raise BoundaryError("caller is fixed to default")
    source_id = manifest.get("source_id", "agent-import")
    if not isinstance(source_id, str) or not source_id.isidentifier():
        raise BoundaryError("invalid source_id")
    rows = _manifest_rows(manifest)
    roots = manifest.get("documentation_roots")
    if not isinstance(roots, list) or not roots or any(not isinstance(root, str) or
                                                       not root or root.startswith("/")
                                                       for root in roots):
        raise BoundaryError("manifest requires explicit documentation_roots")
    plan = {source_id: {"kind": "import", "product_id": product, "version": version,
                         "documentation_roots": roots}}
    parent_run_id = run_id
    controller_run_id = _controller_run_id or parent_run_id
    controller = AcquisitionController(controller_run_id, corpus_root, plan)
    controller._state["parent_run_id"] = parent_run_id
    occurrences = []
    classifications = {}
    for number, row in enumerate(rows, 1):
        rel = row["path"]
        path = source_root / rel
        if not path.is_file() or path.is_symlink() or not path.absolute().is_relative_to(corpus_root):
            if not dev_instance or not path.absolute().is_relative_to(source_root):
                raise BoundaryError("manifest file is outside caller corpus")
        probe = source_root
        for part in Path(rel).parts:
            probe = probe / part
            if probe.is_symlink():
                raise BoundaryError("manifest file uses a symlink")
        data = read_safe(source_root, rel)
        if dev_instance:
            destination = corpus_root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_file() or _digest(destination) != row["sha256"]:
                    raise BoundaryError("dev corpus source collision")
            else:
                shutil.copyfile(path, destination)
            path = destination
        if _digest(path) != row["sha256"] or sha(data) != row["sha256"]:
            raise BoundaryError("source hash mismatch: " + rel)
        boundary = any(rel == root or rel.startswith(root.rstrip("/") + "/") for root in roots)
        decision = classify_payload(data, path=rel, mime_type=row.get("mime_type"),
                                   documentation_boundary=boundary)
        occurrence_id = f"{source_id}:{number}:{row['sha256']}"
        occurrences.append({"id": occurrence_id, "status": "collected", "enumerator_ids": [source_id],
                            "source_path": rel, "path": rel, "source_url": row["canonical_url"],
                            "sha256": row["sha256"], "version": version})
        classifications[occurrence_id] = decision.as_dict()
    controller._state["sources"][source_id] = {"status": "complete", "product_id": product,
        "version": version, "occurrences": occurrences, "dependencies": []}
    controller._state["import_classifications"] = classifications
    return controller, {"manifest": manifest, "source_id": source_id, "bank": bank,
                        "product": product, "version": version, "caller": DEFAULT_CALLER,
                        "parent_run_id": parent_run_id, "controller_run_id": controller_run_id,
                        "corpus_root": str(corpus_root), "dev_instance": dev_instance,
                        "source_corpus_root": str(source_root)}


def prepare(manifest_path: str | Path, *, workspace=None, corpus=None, transport=None,
            dev_instance: bool = False, run_id: str | None = None):
    controller, meta = import_manifest(
        manifest_path, workspace=workspace, corpus=corpus, dev_instance=dev_instance,
        run_id=run_id)
    try:
        ingestion = ControllerIngestion(controller, meta["bank"], transport or _NoNetwork(),
                                        caller=DEFAULT_CALLER)
    except BoundaryError as exc:
        # Keep the legacy run key for the first component. Namespace only when
        # durable state proves this parent run already belongs to another plan.
        if str(exc) != "durable run plan binding mismatch":
            raise
        component_run_id = _component_run_id(meta["parent_run_id"], meta["source_id"],
                                             meta["product"], meta["version"])
        controller, meta = import_manifest(
            manifest_path, workspace=workspace, corpus=corpus, dev_instance=dev_instance,
            run_id=run_id, _controller_run_id=component_run_id)
        ingestion = ControllerIngestion(controller, meta["bank"], transport or _NoNetwork(),
                                        caller=DEFAULT_CALLER)
    setattr(ingestion, "_dev_instance", dev_instance)
    artifacts = ingestion.prepare(meta["source_id"])
    return controller, ingestion, meta, artifacts


class _NoNetwork:
    def __call__(self, request):
        raise BoundaryError("network transport unavailable in prepare/dry-run")


def canonical_source_url(provenance: Any) -> str | None:
    """Deterministic canonical URL for bytes that may be served at several URLs.

    Identical content legitimately appears at multiple locations (a LICENSE
    repeated per package, a release asset copied into a registry).  Choosing the
    lexicographically first URL keeps bank identity stable and reproducible
    while every URL remains recorded in the retained provenance as an alias.
    Returns ``None`` when no source URL is recorded at all, which stays a hard
    reconciliation block.
    """
    if not isinstance(provenance, list):
        return None
    urls: list[str] = []
    for item in provenance:
        if not isinstance(item, dict):
            continue
        value = item.get("source_url")
        if isinstance(value, str) and value and value not in urls:
            urls.append(value)
    urls.sort()
    return urls[0] if urls else None


def bank_gate(ingestion: ControllerIngestion, artifact_id: str, transport,
              meta: dict[str, Any], ledger: dict[str, Any], *,
              inventory_max_pages: int = 2, inventory_detail_reads: int = 0,
              deadline: float | None = None) -> Any:
    """Gate one prepared artifact using its bytes and canonical source URL."""
    artifact, data = ingestion._artifact(artifact_id)
    provenance = artifact.get("provenance", [])
    source_url = canonical_source_url(provenance)
    if source_url is None:
        return PreflightResult(PreflightDisposition.UNKNOWN,
                               "artifact provenance lacks a canonical source URL")
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != artifact.get("sha256"):
        raise BoundaryError("prepared artifact hash mismatch")
    key = CorpusKey(DEFAULT_CALLER, DEFAULT_BANK, meta["product"], meta["version"], source_url, actual_hash)
    reader = meta.get("_bank_inventory_reader")
    if reader is None:
        inventory_reader = HindsightInventoryReader(
            transport, DEFAULT_BANK, caller=DEFAULT_CALLER, ledger=ledger,
            max_pages=inventory_max_pages, detail_reads=inventory_detail_reads,
        )

        # Keep scan receipts in the corpus dedupe area, isolated by caller/bank.
        # The checkpoint is coverage state, not a claim that every row is verified.
        checkpoint = (Path(meta["corpus_root"]) / "dedupe" / "inventory-checkpoints" /
                      f"{DEFAULT_CALLER}--{DEFAULT_BANK}.json")
        checkpoint_state = {}
        snapshot: dict[str, Any] = {}

        def reader(requested):
            if requested is not None and (requested.caller != DEFAULT_CALLER or
                                          requested.bank != DEFAULT_BANK):
                raise BoundaryError("inventory request scope mismatch")
            # Acquire one immutable result for this invocation.  Caching only the
            # callable still re-runs checkpoint reads for every artifact.
            if "result" not in snapshot and "error" not in snapshot:
                try:
                    checkpoint_result = inventory_reader.inventory_checkpoint(
                        checkpoint, ticks=inventory_max_pages)
                    documents = []
                    for row in checkpoint_result.documents:
                        item = inventory_reader._normalise(row)
                        if not item.product or not item.version or not item.source or not item.content_hash:
                            verified = False
                        else:
                            verified = inventory_reader._ledger_proves(item)
                        documents.append(BankDocument(
                            item.product or "", item.version or "", item.source or "",
                            item.content_hash or "", verified, DEFAULT_BANK))
                    snapshot["result"] = BankReaderResult(
                        tuple(documents), complete=checkpoint_result.complete,
                        has_more=not checkpoint_result.complete)
                    # Coverage and preflight use the exact same checkpoint read.
                    checkpoint_state["result"] = checkpoint_result
                except Exception as exc:
                    # A failed first acquisition remains UNKNOWN; do not retry
                    # against a changing bank during this invocation.
                    snapshot["error"] = exc
            if "error" in snapshot:
                raise snapshot["error"]
            return snapshot["result"]
        meta["_bank_inventory_reader"] = reader
        meta["_inventory_checkpoint_state"] = checkpoint_state
    return assess_reuse(key, bank_reader=reader, ledger=ledger)


def submit_with_gate(ingestion: ControllerIngestion, artifact_id: str, transport,
                     meta: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    """Run bank-first preflight; UNKNOWN is a hard no-write decision."""
    if meta.get("bank") != DEFAULT_BANK:
        raise BoundaryError("bank is fixed to Liv")
    if ingestion.caller != DEFAULT_CALLER:
        raise BoundaryError("caller is fixed to default")
    result = bank_gate(ingestion, artifact_id, transport, meta, ledger)
    if result.disposition is PreflightDisposition.UNKNOWN:
        raise BoundaryError("bank inventory is UNKNOWN; no submission permitted")
    if result.disposition is PreflightDisposition.VERIFIED_PRESENT:
        return {"status": "verified-present", "reason": result.reason}
    if result.disposition is PreflightDisposition.INGESTION_PENDING:
        return {"status": "ingestion-pending", "reason": result.reason}
    return ingestion.submit(artifact_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "dry-run", "submit", "resume", "status"))
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    parser.add_argument("--corpus")
    parser.add_argument("--allow-submit", action="store_true", help="required for submit/resume")
    parser.add_argument("--dev-instance", action="store_true",
                        help="bind explicitly to the isolated local Hindsight instance")
    parser.add_argument("--run-id", help="trusted supervisor run identity")
    parser.add_argument("--ledger", type=Path, help="normalized existing claim ledger")
    parser.add_argument("--inventory-max-pages", type=int, default=2)
    parser.add_argument("--inventory-detail-reads", type=int, default=0)
    parser.add_argument("--status-deadline-seconds", type=float, default=30.0,
                        help="bounded wall-clock budget for one read-only status scan")
    parser.add_argument("--max-status-checks", type=int, default=None,
                        help="bounded operation polls per resume invocation (1-256)")
    return parser


def _secret(path: Path = HERMES_ENV) -> str:
    """Read only the authorized key line; never expose its value."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("HINDSIGHT_API_KEY="):
                value = line.split("=", 1)[1].strip().strip("'\"")
                if value:
                    return value
    except OSError:
        pass
    # The plaintext copy now lives in Infisical, behind this profile's read-only identity.
    # Resolve it the same way the gateway does before failing closed. Only the production
    # env file may fall back: the dev-bound instance must never borrow the production key.
    if path == HERMES_ENV:
        try:
            import subprocess
            resolved = subprocess.run(["/home/jsondarula/.hermes/bin/liv-secret", "HINDSIGHT_API_KEY"],
                                      capture_output=True, text=True, timeout=15)
            if resolved.returncode == 0 and resolved.stdout.strip():
                return resolved.stdout.strip()
        except Exception:
            pass
    raise BoundaryError("credential unavailable")


def _transport(allow_submit: bool, *, timeout_seconds: float = 15, dev_instance: bool = False):
    if not allow_submit:
        return _NoNetwork()
    if dev_instance:
        return HindsightHTTPTransport(DEV_ENDPOINT, _secret(DEV_ENV), timeout_seconds=timeout_seconds)
    return HindsightHTTPTransport("http://127.0.0.1:8888", _secret(), timeout_seconds=timeout_seconds)


class _DeadlineTransport:
    """Keep each upstream request inside the single status deadline."""
    def __init__(self, transport, deadline: float):
        self.transport = transport
        self.deadline = deadline

    def __call__(self, request):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("status deadline exceeded")
        if hasattr(self.transport, "timeout_seconds"):
            self.transport.timeout_seconds = min(self.transport.timeout_seconds, remaining)
        return self.transport(request)


def _ledger(path: Path | None, ingestion: ControllerIngestion) -> dict[str, Any]:
    if getattr(ingestion, "_dev_instance", False):
        if path is not None and not path.absolute().is_relative_to(DEV_CORPUS.absolute()):
            raise BoundaryError("dev ledger must be inside dev corpus")
    if path is not None:
        return _json(path.resolve())

    # Read the adapter's durable hash claims, not just this run's receipt map.
    # Claims are keyed by caller, bank, and prepared bytes, so source aliases and
    # run IDs must not make an existing in-flight claim look missing.
    claims = {}
    with ingestion._ledger_lock():
        rows = ingestion._db.execute(
            "SELECT sha, receipt FROM claims WHERE caller=? AND bank=?",
            (DEFAULT_CALLER, DEFAULT_BANK),
        ).fetchall()
    for digest, encoded in rows:
        try:
            record = json.loads(encoded)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        context = record.get("context") if isinstance(record.get("context"), dict) else {}
        state = record.get("status")
        if state == "accepted":
            # A verified operation acceptance is still in-flight until a real
            # completed receipt or bank document proves completion.
            state = "pending"
        # The preflight ledger deliberately carries no source URL: a durable
        # byte claim is the alias-independent fact, while provenance stays in the
        # actual receipt and is never invented here.
        claim = {"sha256": digest, "caller": DEFAULT_CALLER, "bank": DEFAULT_BANK,
                 "state": state}
        for field in ("product", "version"):
            if isinstance(context.get(field), str):
                claim[field] = context[field]
        claims[digest] = claim
    return {"claims": claims}


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    # CLI remains offline by default.  Credential-bearing transports are injected
    # by the trusted caller, not read from arbitrary profiles or prompted for.
    try:
        # Read-only status/resume also use the broker so the bank gate is real;
        # neither path has a submission budget.
        if args.status_deadline_seconds <= 0:
            raise ValueError("status deadline must be positive")
        deadline = (time.monotonic() + args.status_deadline_seconds
                    if args.command == "status" else None)
        transport = _transport(args.allow_submit or args.command in ("status", "resume"),
                               timeout_seconds=min(15, 30), dev_instance=args.dev_instance)
        if deadline is not None:
            transport = _DeadlineTransport(transport, deadline)
        controller, ingestion, meta, artifacts = prepare(args.manifest, workspace=args.workspace,
                                                         corpus=args.corpus, transport=transport,
                                                         dev_instance=args.dev_instance,
                                                         run_id=args.run_id)
        # Tag scheme v2: derive and report the set every artifact will carry, so a
        # run proves its own compliance without a bank read-back.
        tag_sets = artifact_tag_sets(ingestion, artifacts)
        tag_compliance = summarize_tag_sets(tag_sets)

        if args.command in ("prepare", "dry-run"):
            print(json.dumps({"status": "prepared", "artifacts": artifacts,
                              "tags": tag_sets, "tag_compliance": tag_compliance},
                             sort_keys=True))
            return 0
        ledger = _ledger(args.ledger, ingestion)
        if args.command == "status":
            # Receipts are local evidence and are emitted before bounded bank coverage.
            results = []
            for artifact in artifacts:
                receipt = ingestion.state.get("receipts", {}).get(artifact["id"])
                result = bank_gate(
                    ingestion, artifact["id"], transport, meta, ledger,
                    inventory_max_pages=args.inventory_max_pages,
                    inventory_detail_reads=args.inventory_detail_reads,
                    deadline=deadline,
                )
                reader = meta.get("_bank_inventory_reader")
                inventory_state = meta.get("_inventory_checkpoint_state", {})
                inventory = inventory_state.get("result")
                coverage = {
                    "scanned": len(inventory.documents) if inventory is not None else 0,
                    "offset": inventory.offset if inventory is not None else 0,
                    "total": inventory.total if inventory is not None else None,
                    "complete": bool(inventory.complete) if inventory is not None else False,
                    "has_more": not bool(inventory.complete) if inventory is not None else True,
                    "grew_during_scan": bool(getattr(inventory, "snapshot_grew", False)),
                    "max_pages": args.inventory_max_pages,
                    "detail_reads": args.inventory_detail_reads,
                }
                results.append({"artifact_id": artifact["id"], "local_receipt": receipt,
                                "tags": tag_sets.get(artifact["id"], []),
                                "bank": result.__dict__, "bank_coverage": coverage})
            print(json.dumps({"status": "read-only", "results": results,
                              "tag_compliance": tag_compliance}, sort_keys=True))
            return 0
        if args.command == "resume":
            status_checks = args.max_status_checks
            if status_checks is not None and not 1 <= status_checks <= 256:
                raise BoundaryError("max-status-checks must be between 1 and 256")
            result = ingestion.resume(
                max_submissions=0,
                max_status_checks=status_checks if status_checks is not None else ingestion.max_polls)
            print(json.dumps({"status": "status-only", **result}, sort_keys=True))
            return 0
        if args.command == "submit" and not args.allow_submit:
            raise BoundaryError("submit requires --allow-submit")
        results = []
        for artifact in artifacts:
            row = submit_with_gate(ingestion, artifact["id"], transport, meta, ledger)
            if isinstance(row, dict):
                row.setdefault("tags", tag_sets.get(artifact["id"], []))
            results.append(row)
        print(json.dumps({"status": "submitted", "results": results,
                          "tag_compliance": tag_compliance}, sort_keys=True))
        return 0
    except (BoundaryError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "category": type(exc).__name__,
                          "reason": "operation unavailable" if not isinstance(exc, BoundaryError)
                          else str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        # Transport and parser failures are reported by type only; credentials,
        # URLs, response bodies, and request details never cross this boundary.
        print(json.dumps({"status": "blocked", "category": type(exc).__name__,
                          "reason": "operation unavailable"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
