"""Narrow upstream Hindsight proxy policy for lease-bound requests."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import quote

from .core import AuthorizationError, Binding, LeaseBroker


@dataclass(frozen=True)
class UpstreamRequest:
    method: str
    path: str
    body: bytes | None = None
    content_type: str | None = None


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    body: bytes
    content_type: str = "application/json"


class ProxyPolicy:
    """Translate logical broker operations into fixed bank-scoped routes."""

    _OPERATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

    def request(self, operation: str, bank: str, *, operation_id: str = "", body: bytes | None = None,
                content_type: str | None = None) -> UpstreamRequest:
        encoded_bank = quote(bank, safe="")
        if operation == "bank_list":
            return UpstreamRequest("GET", "/v1/default/banks")
        if operation == "retain":
            if body is None or not content_type or not content_type.lower().startswith("multipart/form-data"):
                raise AuthorizationError("retain requires multipart body")
            return UpstreamRequest("POST", f"/v1/default/banks/{encoded_bank}/files/retain", body, content_type)
        if operation == "operation_status":
            if not self._OPERATION_ID.fullmatch(operation_id):
                raise AuthorizationError("invalid operation id")
            return UpstreamRequest("GET", f"/v1/default/banks/{encoded_bank}/operations/{operation_id}")
        if operation == "recall":
            if body is None or content_type != "application/json":
                raise AuthorizationError("recall requires JSON body")
            return UpstreamRequest("POST", f"/v1/default/banks/{encoded_bank}/memories/recall", body, content_type)
        raise AuthorizationError("unsupported upstream operation")


class HindsightProxy:
    """Authorize a lease then forward a narrow request through an injected transport."""

    def __init__(self, broker: LeaseBroker, transport) -> None:
        self.broker = broker
        self.transport = transport
        self.policy = ProxyPolicy()

    def forward(self, lease_id: str, secret: str, binding: Binding, operation: str, **kwargs) -> UpstreamResponse:
        self.broker.authorize(lease_id, secret, binding, operation)
        request = self.policy.request(operation, binding.hindsight_bank, **kwargs)
        return self.transport(request)
