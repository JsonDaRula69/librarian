"""HTTP transport that keeps the upstream Hindsight credential broker-side."""
from __future__ import annotations

import http.client
import ipaddress
import re
import urllib.parse
import urllib.error
import urllib.request

from .proxy import UpstreamRequest, UpstreamResponse


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_DOCUMENT_OFFSET = 10_000_000


class UpstreamTransportError(Exception):
    """Raised for network failures or unsafe upstream configuration."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HindsightHTTPTransport:
    """Forward fixed proxy requests with a broker-held static credential."""

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 30.0) -> None:
        try:
            parsed = urllib.parse.urlsplit(base_url)
            if (not re.fullmatch(r"http://(?:[0-9.]+|\[[0-9a-fA-F:]+\]):[0-9]+/?", base_url)
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                    or not parsed.port or not parsed.hostname
                    or not ipaddress.ip_address(parsed.hostname).is_loopback):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("upstream Hindsight must be numeric loopback HTTP with an explicit port") from None
        if not api_key:
            raise ValueError("upstream API key required")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("invalid upstream timeout")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        # Do not inherit process-global openers or environment proxy settings.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __call__(self, request: UpstreamRequest) -> UpstreamResponse:
        raw_path = request.path
        if not isinstance(raw_path, str):
            raise UpstreamTransportError("unsafe upstream request path")

        # Queries are permitted only for the read-only document listing, and are
        # parsed before the path checks so they cannot alter URL interpretation.
        path, separator, query = raw_path.partition("?")
        if separator:
            if request.method != "GET" or "#" in query or query.count("?"):
                raise UpstreamTransportError("unsafe upstream request query")
            if not path.endswith("/documents"):
                raise UpstreamTransportError("unsafe upstream request query")
            keys = [part.split("=", 1)[0] for part in query.split("&")]
            if (not query or "%" in query or len(keys) != len(set(keys))
                    or not re.fullmatch(r"(?:limit=[0-9]+&offset=[0-9]+|offset=[0-9]+&limit=[0-9]+)", query)):
                raise UpstreamTransportError("unsafe upstream request query")
            values = dict(urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True))
            if set(values) != {"limit", "offset"}:
                raise UpstreamTransportError("unsafe upstream request query")
            try:
                limit = int(values["limit"])
                offset = int(values["offset"])
            except (TypeError, ValueError):
                raise UpstreamTransportError("unsafe upstream request query") from None
            if not 1 <= limit <= 100 or not 0 <= offset <= MAX_DOCUMENT_OFFSET:
                raise UpstreamTransportError("unsafe upstream request query")
        elif "#" in raw_path:
            raise UpstreamTransportError("unsafe upstream request path")

        # Reject alternate URL forms and encoded separators before attaching auth.
        if (not path.startswith("/") or "//" in path
                or re.search(r"[^\x21-\x7e]|[\\?#]", path)
                or re.search(r"%(?![0-9a-fA-F]{2})|%(?:2f|5c|25)", path, re.I)):
            raise UpstreamTransportError("unsafe upstream request path")
        decoded = urllib.parse.unquote(path)
        if (re.search(r"[\x00-\x1f\x7f\\?#]", decoded)
                or any(part in (".", "..") for part in decoded.split("/"))):
            raise UpstreamTransportError("unsafe upstream request path")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if request.content_type:
            headers["Content-Type"] = request.content_type
        upstream = urllib.request.Request(
            self.base_url + request.path,
            data=request.body,
            headers=headers,
            method=request.method,
        )
        try:
            try:
                response = self._opener.open(upstream, timeout=self.timeout_seconds)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                # Bound actual reads, including chunked and error responses.
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise UpstreamTransportError("upstream response exceeds size limit")
                return UpstreamResponse(
                    response.status,
                    body,
                    response.headers.get("Content-Type", "application/octet-stream"),
                )
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
            # Never include request headers or the credential in the exception.
            raise UpstreamTransportError(f"upstream request failed: {type(exc).__name__}") from None
