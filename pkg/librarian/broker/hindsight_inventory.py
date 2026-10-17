"""Read-only, bank-scoped Hindsight document inventory and identity lookup."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from .corpus_preflight import BankDocument, BankReaderResult, CorpusKey
from .proxy import UpstreamRequest

_PAGE_SIZE = 100
_DEFAULT_MAX_PAGES = 100
_DEFAULT_DETAIL_READS = 100
# Artifact-scoped digest keys only: a generic ``sha256`` in a retained context
# may describe a source file rather than the submitted payload.
_CONTEXT_DIGEST_KEYS = ("artifact_sha256", "artifact_hash", "content_sha256", "submitted_sha256")
_CONTEXT_CONTAINER_KEYS = ("metadata", "document_metadata", "retain_params")


class BankInventoryError(RuntimeError):
    """A sanitized read failure or inconsistent inventory response."""


@dataclass(frozen=True)
class InventoryDocument:
    document_id: str | None
    product: str | None
    version: str | None
    source: str | None
    content_hash: str | None
    status: str | None
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class DocumentIdentityResult:
    """Result of a document identity probe; only ``missing`` proves absence."""
    status: str  # present, missing, or unknown
    document_id: str | None = None
    document: InventoryDocument | None = None
    reason: str = ""


@dataclass(frozen=True)
class InventoryCheckpoint:
    """Durable receipt for an incremental, offset-based list scan.

    ``snapshot_grew`` records that the bank's reported total increased while the
    pass was running.  That is tolerated (see ``inventory_checkpoint``) and
    reported, never silently discarded: the proof covers the rows that existed
    when the pass started.
    """

    bank: str
    total: int | None
    offset: int
    documents: tuple[Mapping[str, Any], ...]
    complete: bool
    reason: str = ""
    prior_documents: tuple[Mapping[str, Any], ...] = ()
    snapshot_grew: bool = False


class HindsightInventoryReader:
    """Read documents from exactly one fixed Hindsight bank."""

    def __init__(self, transport, bank: str, *, caller: str | None = None,
                 ledger: Mapping[str, Any] | None = None,
                 max_pages: int = _DEFAULT_MAX_PAGES,
                 detail_reads: int = _DEFAULT_DETAIL_READS):
        if not isinstance(bank, str) or not bank or any(c in bank for c in "\\\x00\r\n"):
            raise ValueError("invalid bank")
        if caller is not None and (not isinstance(caller, str) or not caller):
            raise ValueError("invalid caller")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages must be a positive integer")
        if isinstance(detail_reads, bool) or not isinstance(detail_reads, int) or detail_reads < 0:
            raise ValueError("detail_reads must be a non-negative integer")
        self._transport, self.bank, self.caller = transport, bank, caller
        self._ledger, self.max_pages, self.detail_reads = ledger or {}, max_pages, detail_reads

    def _request(self, path: str) -> tuple[int, Any]:
        try:
            response = self._transport(UpstreamRequest("GET", path))
            status = response.status
            if status < 200 or status >= 300:
                return status, None
            return status, json.loads(response.body.decode("utf-8"))
        except Exception as exc:
            raise BankInventoryError(f"bank inventory read failed: {type(exc).__name__}") from None

    def _get(self, path: str) -> Any:
        status, body = self._request(path)
        if status < 200 or status >= 300:
            raise BankInventoryError(f"bank inventory HTTP {status}")
        return body

    @staticmethod
    def _page(body: Any) -> tuple[list[Mapping[str, Any]], int]:
        if not isinstance(body, Mapping) or set(("items", "total", "limit", "offset")) - body.keys():
            raise BankInventoryError("invalid bank inventory response")
        rows, total = body["items"], body["total"]
        if (not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows)
                or isinstance(total, bool) or not isinstance(total, int) or total < 0):
            raise BankInventoryError("invalid bank inventory page")
        if any(isinstance(body[k], bool) or not isinstance(body[k], int) or body[k] < 0
               for k in ("limit", "offset")):
            raise BankInventoryError("invalid bank inventory page")
        return rows, total

    @staticmethod
    def _value(row: Mapping[str, Any], *names: str) -> str | None:
        containers = [row]
        for key in ("metadata", "document_metadata", "retain_params"):
            value = row.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
        for container in containers:
            for name in names:
                value = container.get(name)
                if isinstance(value, str) and value:
                    return value
        return None

    @classmethod
    def _context_containers(cls, row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """Bank row plus every JSON-encoded context mapping it carries.

        The caller's retained context (``artifact_sha256``, ``product``,
        ``version``, ``caller``) is submitted as a JSON string on the row, so
        identity fields are only visible after parsing it.
        """
        containers: list[Mapping[str, Any]] = [row]
        for key in _CONTEXT_CONTAINER_KEYS:
            value = row.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
        for container in list(containers):
            for value in container.values():
                if not isinstance(value, str) or not value.lstrip().startswith("{"):
                    continue
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, Mapping):
                    containers.append(parsed)
        return containers

    @classmethod
    def _context_digest(cls, row: Mapping[str, Any]) -> str | None:
        """Artifact digest the submitter recorded on the bank row itself.

        Hindsight's own ``content_hash`` covers the retained payload, which is
        not always byte-identical to the prepared artifact.  The retained
        context names the artifact digest that produced the document, so it is
        bank-held evidence of those exact bytes.  JSON-encoded context strings
        are parsed; only artifact-scoped keys are accepted.
        """
        for container in cls._context_containers(row):
            for name in _CONTEXT_DIGEST_KEYS:
                value = container.get(name)
                if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
                    return value
        return None

    @classmethod
    def _context_field(cls, row: Mapping[str, Any], *names: str) -> str | None:
        """Caller identity recorded in the retained context, never inferred.

        Product/version conflicts on hash-identical bytes must be visible to the
        reuse check; without this the bank's own metadata can neither confirm nor
        contradict the request.  ``source`` is only taken when the context names
        it explicitly — a provenance URL list is not a canonical source.
        """
        for container in cls._context_containers(row):
            for name in names:
                value = container.get(name)
                if isinstance(value, str) and value:
                    return value
        return None

    def _normalise(self, row: Mapping[str, Any]) -> InventoryDocument:
        return InventoryDocument(
            self._value(row, "id", "document_id", "documentId"),
            (self._value(row, "product", "product_id", "productId")
             or self._context_field(row, "product", "product_id")),
            (self._value(row, "version")
             or self._context_field(row, "version")),
            (self._value(row, "source", "source_id", "sourceId", "source_url")
             or self._context_field(row, "source", "source_id", "source_url")),
            (self._context_digest(row)
             or self._value(row, "content_hash", "sha256", "hash", "contentHash")),
            self._value(row, "status", "state"), row)

    def _path(self) -> str:
        return f"/v1/default/banks/{quote(self.bank, safe='')}/documents"

    @staticmethod
    def _load_checkpoint(path: str | Path, bank: str) -> InventoryCheckpoint:
        try:
            with open(path, encoding="utf-8") as stream:
                value = json.load(stream)
            if value.get("bank") != bank:
                raise ValueError
            docs = value.get("documents", [])
            if not isinstance(docs, list) or any(not isinstance(d, Mapping) for d in docs):
                raise ValueError
            return InventoryCheckpoint(bank, value.get("total"), value["offset"], tuple(docs),
                                       bool(value["complete"]), value.get("reason", ""),
                                       tuple(value.get("prior_documents", ())),
                                       bool(value.get("snapshot_grew", False)))
        except Exception:
            return InventoryCheckpoint(bank, None, 0, (), False)

    @staticmethod
    def _save_checkpoint(path: str | Path, checkpoint: InventoryCheckpoint) -> None:
        payload = {"bank": checkpoint.bank, "total": checkpoint.total, "offset": checkpoint.offset,
                   "documents": list(checkpoint.documents), "complete": checkpoint.complete,
                   "reason": checkpoint.reason, "prior_documents": list(checkpoint.prior_documents),
                   "snapshot_grew": checkpoint.snapshot_grew}
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
        temporary.replace(target)

    def inventory_checkpoint(self, checkpoint_path: str | Path, *, ticks: int = 1) -> InventoryCheckpoint:
        """Advance a durable list scan by at most ``ticks`` GET pages.

        Completeness is a **snapshot** claim: the scan covers the rows that
        existed when the pass started (the total reported on its first page).
        An append-only bank that grows mid-pass is tolerated and reported via
        ``snapshot_grew`` — document ids are append-only and every digest claim
        is recorded before its POST, so a row added after the first page cannot
        carry a hash this gate is trying to disprove.  A *shrinking* bank breaks
        the premise and still forces a restart.
        """
        if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 1:
            raise ValueError("ticks must be a positive integer")
        state = self._load_checkpoint(checkpoint_path, self.bank)
        if state.complete:
            # A cached terminal result must not be used as an absence proof
            # forever.  This includes the empty case: a bank that was empty on
            # its first scan holds content later, and a stale empty proof keeps
            # every already-ingested artifact in reconciliation while reporting
            # genuinely absent ones as present.  An empty checkpoint therefore
            # pays the same single freshness read as any other terminal one; a
            # bank that is still empty answers with total 0 and proves absence.
            try:
                page, page_total = self._page(self._get(f"{self._path()}?limit={_PAGE_SIZE}&offset=0"))
            except BankInventoryError:
                return InventoryCheckpoint(self.bank, state.total, state.offset, state.documents,
                                           False, "checkpoint_freshness_unknown", state.prior_documents)
            cached_ids = [self._value(row, "id", "document_id", "documentId") for row in state.documents]
            fresh_ids = [self._value(row, "id", "document_id", "documentId") for row in page]
            invalid_fresh_ids = (any(ident is None for ident in fresh_ids)
                                 or len(set(fresh_ids)) != len(fresh_ids))
            if (page_total != state.total or invalid_fresh_ids
                    or cached_ids[:len(fresh_ids)] != fresh_ids):
                state = InventoryCheckpoint(self.bank, None, 0, (), False,
                                            "restart_required", state.documents)
                self._save_checkpoint(checkpoint_path, state)
                return InventoryCheckpoint(self.bank, None, 0, state.documents, False,
                                           "restart_required", state.documents)
            else:
                return state
        rows_by_id = {self._value(row, "id", "document_id", "documentId"): row for row in state.documents}
        offset, total, complete = state.offset, state.total, False
        reason = state.reason
        restarted = reason == "restart_required"
        snapshot_grew = state.snapshot_grew
        for _ in range(ticks):
            try:
                body = self._get(f"{self._path()}?limit={_PAGE_SIZE}&offset={offset}")
                page, page_total = self._page(body)
            except BankInventoryError:
                break
            if total is None:
                total = page_total
            elif page_total > total:
                # Append-only growth: keep the snapshot target and finish the pass.
                snapshot_grew = True
            elif page_total < total:
                # Shrinkage is not append-only growth; the snapshot premise is gone.
                reason = "restart_required"
                if not restarted:
                    state = InventoryCheckpoint(self.bank, None, 0, (), False, reason,
                                                tuple(rows_by_id.values()), snapshot_grew)
                    self._save_checkpoint(checkpoint_path, state)
                    return self.inventory_checkpoint(checkpoint_path, ticks=ticks)
                total, offset, rows_by_id, complete = None, 0, {}, False
                break
            page_ids = [self._value(row, "id", "document_id", "documentId") for row in page]
            known_ids = set(rows_by_id)
            if (any(ident is None for ident in page_ids)
                    or len(set(page_ids)) != len(page_ids)
                    or any(ident in known_ids for ident in page_ids)):
                reason = "restart_required"
                if not restarted:
                    state = InventoryCheckpoint(self.bank, None, 0, (), False, reason,
                                                tuple(rows_by_id.values()), snapshot_grew)
                    self._save_checkpoint(checkpoint_path, state)
                    return self.inventory_checkpoint(checkpoint_path, ticks=ticks)
                total, offset, rows_by_id, complete = None, 0, {}, False
                break
            for row in page:
                ident = self._value(row, "id", "document_id", "documentId")
                marker = ident or json.dumps(row, sort_keys=True, default=str)
                rows_by_id.setdefault(marker, row)
            offset += len(page)
            if not page:
                # Only a fresh offset-zero empty bank proves absence.
                if page_total == 0 and offset == 0 and not rows_by_id and not state.prior_documents:
                    complete = True
                    break
                reason = "restart_required"
                if not restarted:
                    state = InventoryCheckpoint(self.bank, None, 0, (), False, reason,
                                                tuple(rows_by_id.values()), snapshot_grew)
                    self._save_checkpoint(checkpoint_path, state)
                    return self.inventory_checkpoint(checkpoint_path, ticks=ticks)
                complete = False
                break
            if total == 0 or offset == total:
                complete = offset == total and len(rows_by_id) == total
                if complete and restarted:
                    # A full pass from offset zero with a stable total and
                    # unique IDs supersedes the stale checkpoint evidence.
                    reason = ""
                break
        result = InventoryCheckpoint(self.bank, total, offset, tuple(rows_by_id.values()), complete,
                                     reason, state.prior_documents, snapshot_grew)
        # A restart is useful repair work and never proves full-bank absence; the
        # completeness claim above is a snapshot of the rows seen since offset zero.
        if reason == "restart_required":
            result = InventoryCheckpoint(self.bank, total, offset, result.documents,
                                         False, reason, result.prior_documents, snapshot_grew)
        self._save_checkpoint(checkpoint_path, result)
        return result

    def _ledger_proves(self, item: InventoryDocument) -> bool:
        if (self.caller is None or not item.document_id or not item.product or not item.version
                or not item.source or not item.content_hash or not re.fullmatch(r"[0-9a-fA-F]{64}", item.content_hash)):
            return False
        claims = self._ledger.get("claims", {}) if isinstance(self._ledger, Mapping) else {}
        values = claims.values() if isinstance(claims, Mapping) else claims if isinstance(claims, list) else ()
        return any(isinstance(c, Mapping) and c.get("document_id", c.get("documentId", c.get("id"))) == item.document_id
                   and c.get("caller") == self.caller and c.get("bank") == self.bank
                   and c.get("product") == item.product and c.get("version") == item.version
                   and c.get("source") == item.source and c.get("sha256", c.get("content_hash")) == item.content_hash
                   and str(c.get("state", "")).lower() in {"completed", "complete"} for c in values)

    def inventory(self) -> BankReaderResult:
        state_path = None
        rows, seen, total, complete, offset = [], set(), None, True, 0
        try:
            for page_number in range(self.max_pages):
                page, page_total = self._page(self._get(f"{self._path()}?limit={_PAGE_SIZE}&offset={offset}"))
                if total is None: total = page_total
                elif page_total != total: complete = False
                for row in page:
                    ident = self._value(row, "id", "document_id", "documentId")
                    if ident is None:
                        complete = False
                    marker = ident or json.dumps(row, sort_keys=True, default=str)
                    if marker in seen: complete = False
                    else: seen.add(marker); rows.append(row)
                offset += len(page)
                if offset >= total: complete = complete and offset == total; break
                if not page or len(page) < _PAGE_SIZE or page_number + 1 == self.max_pages: complete = False; break
            else: complete = False
            complete = complete and total is not None and len(rows) == total
        except BankInventoryError: complete = False
        documents = []
        details_left = self.detail_reads
        for row in rows:
            item = self._normalise(row)
            if item.document_id and (not item.product or not item.version or not item.source or not item.content_hash or not item.status):
                if details_left <= 0: complete = False
                else:
                    details_left -= 1
                    try:
                        detail = self._get(f"{self._path()}/{quote(item.document_id, safe='')}")
                        if not isinstance(detail, Mapping) or detail.get("id") != item.document_id or detail.get("bank_id") not in (None, self.bank): raise BankInventoryError("document identity mismatch")
                        merged = dict(row); merged.update(detail); item = self._normalise(merged)
                    except BankInventoryError: complete = False
            if not item.product or not item.version or not item.source or not item.content_hash: complete = False
            documents.append(BankDocument(item.product or "", item.version or "", item.source or "", item.content_hash or "", self._ledger_proves(item), self.bank))
        return BankReaderResult(tuple(documents), complete=complete, has_more=not complete)

    def lookup_document(self, *, document_id: str | None = None, content_hash: str | None = None,
                        filename: str | None = None, max_pages: int | None = None) -> DocumentIdentityResult:
        """Look up a known identity using list/detail GETs; never equates filename and ID."""
        if sum(value is not None for value in (document_id, content_hash, filename)) != 1:
            raise ValueError("provide exactly one document identity")
        if document_id is not None:
            status, body = self._request(f"{self._path()}/{quote(document_id, safe='')}")
            if status == 200 and isinstance(body, Mapping) and body.get("id") == document_id and body.get("bank_id") in (None, self.bank):
                item = self._normalise(body); return DocumentIdentityResult("present", document_id, item)
            if status == 404: return DocumentIdentityResult("missing", document_id, reason="verified document 404")
            return DocumentIdentityResult("unknown", document_id, reason=f"document detail HTTP {status}" if status else "detail timeout")
        pages = self.max_pages if max_pages is None else max_pages
        if pages < 1: raise ValueError("max_pages must be positive")
        offset = 0
        for _ in range(pages):
            try: page, total = self._page(self._get(f"{self._path()}?limit={_PAGE_SIZE}&offset={offset}"))
            except BankInventoryError as exc: return DocumentIdentityResult("unknown", reason=str(exc))
            for row in page:
                value = self._value(row, "content_hash", "sha256", "hash", "contentHash") if content_hash else self._value(row, "filename", "file_name", "name")
                if value != (content_hash if content_hash else filename): continue
                ident = self._value(row, "id", "document_id", "documentId")
                if not ident: return DocumentIdentityResult("unknown", reason="matching list row has no document id")
                return self.lookup_document(document_id=ident)
            offset += len(page)
            if not page or offset >= total: break
        return DocumentIdentityResult("unknown", reason="inventory incomplete; absence is not proven")

    def __call__(self, requested: CorpusKey | None = None) -> BankReaderResult:
        if requested is not None and (requested.bank != self.bank or (self.caller is not None and requested.caller != self.caller)): raise ValueError("inventory request scope mismatch")
        return self.inventory()


def bank_inventory_reader(transport, bank: str, *, caller: str | None = None, ledger: Mapping[str, Any] | None = None,
                          max_pages: int = _DEFAULT_MAX_PAGES, detail_reads: int = _DEFAULT_DETAIL_READS) -> HindsightInventoryReader:
    return HindsightInventoryReader(transport, bank, caller=caller, ledger=ledger, max_pages=max_pages, detail_reads=detail_reads)
