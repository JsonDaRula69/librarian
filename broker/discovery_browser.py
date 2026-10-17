"""Bounded, read-only web discovery for unverified candidate sources."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import re
from threading import RLock
from urllib.parse import urljoin
import uuid

from . import safe_egress


_DEFAULT_MAX_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_TEXT = 256 * 1024
_DEFAULT_TIMEOUT = 20.0


class _HTMLTextLinks(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"}:
            self._skip += 1
        if tag == "a" and not self._skip:
            href = dict(attrs).get("href")
            if href:
                self.links.append((urljoin(self.base_url, href), "html"))

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript", "template"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.text.append(data)


_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)")


def _markdown_links(text: str, base_url: str) -> list[str]:
    result = []
    for match in _MARKDOWN_LINK.finditer(text):
        target = match.group(1)
        if target.startswith("<"):
            target = target[1:-1]
        result.append(urljoin(base_url, target))
    return result


def _clean_text(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


@dataclass(frozen=True)
class _Receipt:
    receipt_id: str
    requested_url: str
    final_url: str
    status: int
    sha256: str
    raw: bytes
    text: str
    links: tuple[dict[str, object], ...]
    redirects: tuple[str, ...]


class DiscoveryBrowser:
    """Store bounded fetch receipts and expose only receipt-based discovery reads.

    This class deliberately makes no claim that a page is official, complete, or
    authoritative. It is a candidate-source surface for an isolated LLM.
    """

    def __init__(self, *, fetch=None, max_bytes: int = _DEFAULT_MAX_BYTES,
                 timeout: float = _DEFAULT_TIMEOUT, max_text: int = _DEFAULT_MAX_TEXT,
                 max_redirects: int = 5) -> None:
        if type(max_bytes) is not int or not 0 <= max_bytes <= 128 * 1024 * 1024:
            raise ValueError("invalid max_bytes")
        if type(max_text) is not int or not 0 <= max_text <= 16 * 1024 * 1024:
            raise ValueError("invalid max_text")
        if type(max_redirects) is not int or not 0 <= max_redirects <= 10:
            raise ValueError("invalid max_redirects")
        self._fetch = fetch or safe_egress.fetch
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.max_text = max_text
        self.max_redirects = max_redirects
        self._receipts: dict[str, _Receipt] = {}
        self._lock = RLock()

    def fetch(self, url: str) -> dict[str, object]:
        """Fetch one public HTTPS URL through safe_egress and retain its receipt."""
        result = self._fetch(url, max_bytes=self.max_bytes, timeout=self.timeout,
                             max_redirects=self.max_redirects)
        raw = bytes(result.data)
        final_url = result.url
        content = raw.decode("utf-8", "replace")
        parser = _HTMLTextLinks(final_url)
        is_html = re.search(r"<\s*(?:html|body|a|p|div)\b", content, re.I)
        if is_html:
            parser.feed(content)
            text = _clean_text(parser.text)
            candidates = [u for u, _ in parser.links]
        else:
            text = content
            candidates = _markdown_links(content, final_url)
        text = text[: self.max_text]
        receipt_id = uuid.uuid4().hex
        links = tuple({
            "url": target,
            "source_receipt_id": receipt_id,
            "ordinal": ordinal,
            "discovered_from": final_url,
        } for ordinal, target in enumerate(dict.fromkeys(candidates)))
        receipt = _Receipt(receipt_id, url, final_url, int(result.status),
                           hashlib.sha256(raw).hexdigest(), raw, text, links,
                           tuple(getattr(result, "redirects", ())))
        with self._lock:
            self._receipts[receipt_id] = receipt
        return {
            "receipt_id": receipt_id,
            "requested_url": url,
            "final_url": final_url,
            "http_status": receipt.status,
            "sha256": receipt.sha256,
            "redirects": receipt.redirects,
            "authority": "unverified_candidate",
            "text_length": len(text),
            "truncated": len(text) >= self.max_text,
        }

    def _get(self, receipt_id: str) -> _Receipt:
        with self._lock:
            try:
                return self._receipts[receipt_id]
            except KeyError as exc:
                raise KeyError("unknown receipt_id") from exc

    def read(self, receipt_id: str, offset: int, limit: int) -> dict[str, object]:
        if type(offset) is not int or type(limit) is not int or offset < 0 or limit < 0:
            raise ValueError("offset and limit must be non-negative integers")
        receipt = self._get(receipt_id)
        chunk = receipt.text[offset:offset + limit]
        return {"receipt_id": receipt_id, "offset": offset, "limit": limit,
                "text": chunk, "next_offset": offset + len(chunk),
                "truncated": offset + len(chunk) < len(receipt.text)}

    def enumerate_links(self, receipt_id: str) -> list[dict[str, object]]:
        return [dict(link) for link in self._get(receipt_id).links]
