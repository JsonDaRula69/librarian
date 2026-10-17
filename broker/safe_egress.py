"""Public HTTPS GET with DNS answers pinned to numeric socket connections.

No proxy, cookie jar, netrc, caller headers, or ambient TLS trust overrides.
Tests replace socket operations, not the production address policy. OS egress
is still required as defense in depth (routing/NAT is outside this module).
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import io
import ipaddress
import math
import os
import queue
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit


class EgressError(ValueError):
    pass


@dataclass(frozen=True)
class FetchResult:
    data: bytes
    url: str
    status: int
    redirects: tuple[str, ...]


_DNS_SLOTS = threading.BoundedSemaphore(8)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('HTTPS acquisition deadline exceeded')
    return remaining


def validate_url(url):
    if (not isinstance(url, str) or not url or len(url) > 8192
            or any(ord(c) <= 32 or ord(c) >= 127 for c in url)
            or '\\' in url or '#' in url):
        raise EgressError('invalid HTTPS URL')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or '@' in parsed.netloc
                or '%' in parsed.netloc or parsed.port == 0 or parsed.netloc.endswith(':')):
            raise ValueError()
        port = parsed.port or 443
    except ValueError as exc:
        raise EgressError('public HTTPS without credentials required') from exc
    return parsed, port


def _public_ip(value):
    ip = ipaddress.ip_address(value)
    if (not ip.is_global or ip.is_multicast or ip.is_reserved
            or ip.is_loopback or ip.is_link_local or ip.is_unspecified
            or getattr(ip, 'ipv4_mapped', None) is not None
            or getattr(ip, 'sixtofour', None) is not None
            or getattr(ip, 'teredo', None) is not None
            or (ip.version == 6 and ip in ipaddress.ip_network('64:ff9b::/96'))):
        raise EgressError('private or reserved network address forbidden')
    return ip


def _resolve(host, port, deadline):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        ip = _public_ip(str(literal))
        return socket.AF_INET if ip.version == 4 else socket.AF_INET6, str(ip)
    # libc DNS has no timeout. Bound waiting and outstanding resolver threads;
    # late answers are discarded and can never initiate a connection.
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise TimeoutError('DNS capacity deadline exceeded')
    result = queue.Queue(maxsize=1)
    def resolve():
        try:
            result.put((socket.getaddrinfo(host, port, type=socket.SOCK_STREAM), None))
        except Exception as exc:
            result.put((None, exc))
        finally:
            _DNS_SLOTS.release()
    threading.Thread(target=resolve, daemon=True).start()
    try:
        answers, error = result.get(timeout=_remaining(deadline))
    except queue.Empty as exc:
        raise TimeoutError('DNS deadline exceeded') from exc
    if error:
        raise error
    if not answers:
        raise EgressError('empty DNS answer')
    checked = []
    for family, kind, protocol, name, address in answers:
        ip = _public_ip(address[0])
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise EgressError('unsupported address family')
        if (family == socket.AF_INET) != (ip.version == 4):
            raise EgressError('address family mismatch')
        checked.append((family, str(ip)))
    return checked[0]


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline, budget):
        self.sock, self.deadline, self.budget = sock, deadline, budget

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_remaining(self.deadline))
        count = self.sock.recv_into(memoryview(buffer)[:min(len(buffer), self.budget + 1)])
        _remaining(self.deadline)
        self.budget -= count
        if self.budget < 0:
            raise EgressError('HTTP wire byte limit exceeded')
        return count


class _ResponseSocket:
    def __init__(self, sock, deadline, budget):
        self.sock, self.deadline, self.budget = sock, deadline, budget

    def makefile(self, mode):
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline, self.budget))


def _tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    paths = ssl.get_default_verify_paths()
    context.load_verify_locations(
        cafile=paths.openssl_cafile if os.path.isfile(paths.openssl_cafile) else None,
        capath=paths.openssl_capath if os.path.isdir(paths.openssl_capath) else None)
    context.set_alpn_protocols(['http/1.1'])
    return context


def fetch(url, *, max_bytes=32 * 1024 * 1024, timeout=20.0,
          max_redirects=5, validate=None):
    """Fetch one bounded document; validate is an additional boundary restriction."""
    if (type(max_bytes) is not int or max_bytes < 0 or max_bytes > 128 * 1024 * 1024
            or type(max_redirects) is not int or not 0 <= max_redirects <= 10
            or not math.isfinite(timeout) or not 0 < timeout <= 120):
        raise EgressError('invalid acquisition limits')
    deadline = time.monotonic() + timeout
    redirects = []
    context = _tls_context()
    while True:
        parsed, port = validate_url(url)
        if validate is not None:
            validate(url)
        family, address = _resolve(parsed.hostname, port, deadline)
        raw = socket.socket(family, socket.SOCK_STREAM)
        tls = None
        response = None
        try:
            raw.settimeout(_remaining(deadline))
            # Numeric connect on an explicitly selected family never re-resolves.
            raw.connect((address, port) if family == socket.AF_INET else (address, port, 0, 0))
            raw.settimeout(_remaining(deadline))
            tls = context.wrap_socket(raw, server_hostname=parsed.hostname)
            tls.settimeout(_remaining(deadline))
            target = parsed.path or '/'
            if parsed.query:
                target += '?' + parsed.query
            host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
            if port != 443:
                host += f':{port}'
            request = (f'GET {target} HTTP/1.1\r\nHost: {host}\r\n'
                       'Accept-Encoding: identity\r\nConnection: close\r\n'
                       'User-Agent: Librarian-Acquisition/1\r\n\r\n')
            tls.sendall(request.encode('ascii'))
            response = http.client.HTTPResponse(_ResponseSocket(tls, deadline, max_bytes + 65536))
            response.begin()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location or len(redirects) >= max_redirects:
                    raise EgressError('missing location or redirect limit exceeded')
                # Reject raw controls before urljoin can normalize them away.
                if any(ord(c) <= 32 or ord(c) >= 127 for c in location):
                    raise EgressError('invalid redirect location')
                url = urljoin(url, location)
                redirects.append(url)
                continue
            if not 200 <= response.status < 300:
                error = EgressError(f'HTTP status {response.status}')
                error.code = response.status
                raise error
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise EgressError('encoded response forbidden')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) > max_bytes):
                raise EgressError('download size limit exceeded')
            data = response.read(max_bytes + 1)
            _remaining(deadline)
            if len(data) > max_bytes:
                raise EgressError('download size limit exceeded')
            if length is not None and len(data) != int(length):
                raise EgressError('truncated response')
            return FetchResult(data, url, response.status, tuple(redirects))
        except http.client.HTTPException as exc:
            raise EgressError('malformed or incomplete HTTP response') from exc
        finally:
            if response is not None:
                response.close()
            if tls is not None:
                tls.close()
            raw.close()
