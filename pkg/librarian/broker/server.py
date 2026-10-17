"""Loopback-only HTTP boundary for the in-memory Librarian lease broker."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

from .core import AuthorizationError, Binding, LeaseBroker
from .proxy import HindsightProxy


class BrokerHTTPServer(ThreadingHTTPServer):
    """HTTP service that exposes only lease lifecycle and authorization checks."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, broker: LeaseBroker, control_token: str, proxy: HindsightProxy | None = None) -> None:
        host, _ = address
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("broker must bind to loopback")
        self.broker = broker
        self.control_token = control_token
        self.proxy = proxy
        super().__init__(address, BrokerRequestHandler)


class BrokerRequestHandler(BaseHTTPRequestHandler):
    def _broker_server(self) -> BrokerHTTPServer:
        if not isinstance(self.server, BrokerHTTPServer):
            raise RuntimeError("invalid broker server")
        return self.server

    def log_message(self, format: str, *args: object) -> None:
        # Avoid request headers or bodies in logs; callers can add metadata-only logging later.
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self._upstream(status, body, "application/json")

    def _upstream(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw_body(self, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > max_bytes:
                raise ValueError
            return self.rfile.read(length)
        except ValueError:
            raise AuthorizationError("invalid request body") from None

    def _body(self) -> dict[str, Any]:
        try:
            value = json.loads(self._raw_body(65536))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, json.JSONDecodeError):
            raise AuthorizationError("invalid request body")

    def _control(self) -> None:
        supplied = self.headers.get("X-Broker-Control-Token", "")
        if not supplied or not secrets.compare_digest(supplied, self._broker_server().control_token):
            raise AuthorizationError("control authentication required")

    def _lease_credentials(self) -> tuple[str, str]:
        lease_id = self.headers.get("X-Librarian-Lease-Id", "")
        auth = self.headers.get("Authorization", "")
        secret = auth[7:] if auth.startswith("Bearer ") else ""
        if not lease_id or not secret:
            raise AuthorizationError("lease authentication required")
        return lease_id, secret

    @staticmethod
    def _binding(body: dict[str, Any]) -> Binding:
        try:
            return Binding(
                caller_profile=str(body["caller_profile"]),
                workspace=str(body["workspace"]),
                corpus_root=str(body["corpus_root"]),
                hindsight_bank=str(body["hindsight_bank"]),
            )
        except KeyError as exc:
            raise AuthorizationError("binding fields required") from exc

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/v1/hindsight/retain":
                server = self._broker_server()
                if server.proxy is None:
                    raise AuthorizationError("upstream proxy unavailable")
                lease_id, secret = self._lease_credentials()
                values = parse_qs(urlparse(self.path).query)
                binding = Binding(
                    values.get("caller_profile", [""])[0], values.get("workspace", [""])[0],
                    values.get("corpus_root", [""])[0], values.get("hindsight_bank", [""])[0],
                )
                content_type = self.headers.get("Content-Type", "")
                response = server.proxy.forward(
                    lease_id, secret, binding, "retain", body=self._raw_body(), content_type=content_type
                )
                self._upstream(response.status, response.body, response.content_type)
                return
            body = self._body()
            if path == "/v1/leases":
                self._control()
                operations = body.get("operations")
                if not isinstance(operations, list) or not all(isinstance(x, str) for x in operations):
                    raise AuthorizationError("operations must be a string list")
                lease = self._broker_server().broker.issue(
                    self._binding(body),
                    str(body.get("run_id", "")),
                    set(operations),
                    int(body.get("ttl_seconds", 1800)),
                )
                # The secret is returned once and never appears in status metadata.
                self._json(201, {
                    "lease_id": lease.lease_id,
                    "lease_secret": lease.secret,
                    "expires_at": lease.expires_at.isoformat(),
                })
                return
            if path == "/v1/authorize":
                lease_id, secret = self._lease_credentials()
                lease = self._broker_server().broker.authorize(
                    lease_id,
                    secret,
                    self._binding(body),
                    str(body.get("operation", "")),
                )
                self._json(200, {"authorized": True, "lease_id": lease.lease_id})
                return
            if path == "/v1/hindsight/recall":
                server = self._broker_server()
                if server.proxy is None:
                    raise AuthorizationError("upstream proxy unavailable")
                lease_id, secret = self._lease_credentials()
                binding = self._binding(body)
                payload = json.dumps(body.get("request", {}), separators=(",", ":")).encode()
                response = server.proxy.forward(
                    lease_id, secret, binding, "recall", body=payload, content_type="application/json"
                )
                self._upstream(response.status, response.body, response.content_type)
                return
            self._json(404, {"error": "not found"})
        except AuthorizationError as exc:
            self._json(403, {"error": str(exc)})
        except Exception:
            self._json(400, {"error": "invalid request"})

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            values = parse_qs(urlparse(self.path).query)
            if path == "/v1/hindsight/banks":
                operation = "bank_list"
                kwargs = {}
            elif path == "/v1/hindsight/operations":
                operation = "operation_status"
                kwargs = {"operation_id": values.get("operation_id", [""])[0]}
            else:
                operation = ""
                kwargs = {}
            if operation:
                server = self._broker_server()
                if server.proxy is None:
                    raise AuthorizationError("upstream proxy unavailable")
                lease_id, secret = self._lease_credentials()
                binding = Binding(
                    values.get("caller_profile", [""])[0],
                    values.get("workspace", [""])[0],
                    values.get("corpus_root", [""])[0],
                    values.get("hindsight_bank", [""])[0],
                )
                response = server.proxy.forward(lease_id, secret, binding, operation, **kwargs)
                self._upstream(response.status, response.body, response.content_type)
                return
            self._control()
            prefix = "/v1/leases/"
            if not path.startswith(prefix):
                self._json(404, {"error": "not found"})
                return
            self._json(200, self._broker_server().broker.metadata(path[len(prefix):]))
        except AuthorizationError as exc:
            self._json(403, {"error": str(exc)})

    def do_DELETE(self) -> None:
        try:
            self._control()
            path = urlparse(self.path).path
            prefix = "/v1/leases/"
            if not path.startswith(prefix):
                self._json(404, {"error": "not found"})
                return
            body = self._body()
            changed = self._broker_server().broker.revoke(path[len(prefix):], str(body.get("lease_secret", "")))
            self._json(200, {"revoked": changed})
        except AuthorizationError as exc:
            self._json(403, {"error": str(exc)})


class Reaper:
    """Periodically removes expired in-memory leases."""

    def __init__(self, broker: LeaseBroker, interval_seconds: float = 5.0) -> None:
        self.broker = broker
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="librarian-lease-reaper")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(self.interval_seconds * 2, 1))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.broker.reap()
