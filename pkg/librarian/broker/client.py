"""Client used by Librarian to access only lease-bound broker operations."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .core import Binding
from .proxy import UpstreamResponse


class BrokerClientError(Exception):
    """Raised when the local broker cannot serve an authorized request."""


class BrokerClient:
    def __init__(self, base_url: str, lease_id: str, lease_secret: str, binding: Binding, timeout: float = 30.0) -> None:
        if not base_url.startswith("http://127.0.0.1:") and not base_url.startswith("http://localhost:"):
            raise ValueError("broker client must use loopback")
        if not lease_id or not lease_secret:
            raise ValueError("lease credentials required")
        self.base_url = base_url.rstrip("/")
        self.lease_id = lease_id
        self._secret = lease_secret
        self.binding = binding
        self.timeout = timeout

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "X-Librarian-Lease-Id": self.lease_id,
            "Authorization": "Bearer " + self._secret,
            "Accept": "application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _call(self, path: str, method: str = "GET", body: bytes | None = None,
              content_type: str | None = None) -> UpstreamResponse:
        request = urllib.request.Request(
            self.base_url + path, data=body, headers=self._headers(content_type), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return UpstreamResponse(response.status, response.read(), response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            return UpstreamResponse(exc.code, exc.read(), exc.headers.get("Content-Type", "") if exc.headers else "")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise BrokerClientError(f"broker request failed: {type(exc).__name__}") from None

    def bank_list(self) -> UpstreamResponse:
        query = urllib.parse.urlencode({
            "caller_profile": self.binding.caller_profile,
            "workspace": self.binding.workspace,
            "corpus_root": self.binding.corpus_root,
            "hindsight_bank": self.binding.hindsight_bank,
        })
        return self._call("/v1/hindsight/banks?" + query)

    def operation_status(self, operation_id: str) -> UpstreamResponse:
        query = urllib.parse.urlencode({
            "caller_profile": self.binding.caller_profile,
            "workspace": self.binding.workspace,
            "corpus_root": self.binding.corpus_root,
            "hindsight_bank": self.binding.hindsight_bank,
            "operation_id": operation_id,
        })
        return self._call("/v1/hindsight/operations?" + query)

    def recall(self, request_body: dict[str, object]) -> UpstreamResponse:
        body = {
            "caller_profile": self.binding.caller_profile,
            "workspace": self.binding.workspace,
            "corpus_root": self.binding.corpus_root,
            "hindsight_bank": self.binding.hindsight_bank,
            "request": request_body,
        }
        return self._call(
            "/v1/hindsight/recall", "POST", json.dumps(body, separators=(",", ":")).encode(), "application/json"
        )

    def retain(self, multipart_body: bytes, content_type: str) -> UpstreamResponse:
        query = urllib.parse.urlencode({
            "caller_profile": self.binding.caller_profile,
            "workspace": self.binding.workspace,
            "corpus_root": self.binding.corpus_root,
            "hindsight_bank": self.binding.hindsight_bank,
        })
        return self._call("/v1/hindsight/retain?" + query, "POST", multipart_body, content_type)
