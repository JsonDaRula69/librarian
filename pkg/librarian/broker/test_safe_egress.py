"""Offline wire fixtures: never start services or permit local production egress."""
import io
import socket
import ssl
from unittest.mock import patch

import pytest

from broker.acquisition_controller import AcquisitionController, BoundaryError


class WireSocket:
    def __init__(self, wire):
        self.wire = io.BytesIO(wire)
        self.sent = b''
        self.closed = False
        self.address = None

    def settimeout(self, timeout):
        assert timeout > 0

    def connect(self, address):
        self.address = address

    def recv_into(self, buffer):
        return self.wire.readinto(buffer)

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True


def test_pinned_socket_preserves_hostname_tls_and_host(monkeypatch):
    from broker import safe_egress
    wire = WireSocket(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello')
    resolutions = []
    def resolve(host, port, *args, **kwargs):
        resolutions.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]
    tls_names = []
    def wrap(context, raw, *, server_hostname):
        assert context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED
        tls_names.append(server_hostname)
        return raw
    monkeypatch.setenv('https_proxy', 'http://127.0.0.1:99')
    monkeypatch.setenv('SSL_CERT_FILE', '/nonexistent/ambient-cert')
    with patch.object(socket, 'getaddrinfo', side_effect=resolve), patch.object(socket, 'socket', return_value=wire), patch.object(ssl.SSLContext, 'wrap_socket', wrap):
        result = safe_egress.fetch('https://docs.example/docs/a', max_bytes=20)
    assert result.data == b'hello'
    assert resolutions == ['docs.example']
    assert wire.address == ('93.184.216.34', 443)
    assert tls_names == ['docs.example']
    assert b'Host: docs.example\r\n' in wire.sent
    assert b'Authorization' not in wire.sent
    assert wire.closed


def test_controller_uses_pinned_fetch(tmp_path):
    from broker import safe_egress
    wire = WireSocket(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello')
    controller = AcquisitionController('test', tmp_path, {})
    with patch.object(safe_egress, 'fetch', return_value=safe_egress.FetchResult(b'hello', 'https://docs.example/docs/a', 200, ())) as fetch:
        receipt = {'attempts': []}
        assert controller._fetch('docs', 'https://docs.example/docs/a', 'https://docs.example/docs/', receipt) == b'hello'
    assert fetch.call_count == 1
    assert receipt['attempts'][0]['status'] == 'succeeded'


def test_legacy_loopback_flag_cannot_enable_production_http(tmp_path):
    controller = AcquisitionController('test', tmp_path, {}, allow_loopback=True)
    with pytest.raises(BoundaryError):
        controller._url('http://127.0.0.1/docs/a', 'http://127.0.0.1/docs/')


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.1.2.3', '169.254.169.254',
    '192.0.2.1', '100.64.0.1', '224.0.0.1', '240.0.0.1', '::1', 'fe80::1',
    'fc00::1', '2001:db8::1', '::ffff:93.184.216.34', '64:ff9b::a00:1'])
def test_nonpublic_dns_answers_never_connect(ip):
    from broker import safe_egress
    family = socket.AF_INET6 if ':' in ip else socket.AF_INET
    answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
               (family, socket.SOCK_STREAM, 6, '', (ip, 443))]
    with patch.object(socket, 'getaddrinfo', return_value=answers), patch.object(socket, 'socket') as connect:
        with pytest.raises(safe_egress.EgressError):
            safe_egress.fetch('https://docs.example/a')
        connect.assert_not_called()


@pytest.mark.parametrize('target', ['https://127.0.0.1/a', 'http://docs.example/a',
    'https://user:pass@docs.example/a', 'https://[::1]/a'])
def test_redirects_revalidate_before_connection(target):
    from broker import safe_egress
    wire = WireSocket(('HTTP/1.1 302 Found\r\nLocation: ' + target + '\r\n\r\n').encode())
    with patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, 1, 6, '', ('93.184.216.34', 443))]), patch.object(socket, 'socket', return_value=wire) as connect, patch.object(ssl.SSLContext, 'wrap_socket', lambda self, raw, **kw: raw):
        with pytest.raises(safe_egress.EgressError):
            safe_egress.fetch('https://docs.example/a')
        assert connect.call_count == 1
    assert wire.closed


@pytest.mark.parametrize('wire', [
    b'HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\n123456789',
    b'HTTP/1.1 200 OK\r\n\r\n123456789',
    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n9\r\n123456789\r\n0\r\n\r\n',
    b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nx',
    b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\nx',
    b'HTTP/1.1 302 Found\r\nLocation: /b\r\n\r\n'])
def test_size_encoding_truncation_and_redirect_limits(wire):
    from broker import safe_egress
    sock = WireSocket(wire)
    with patch.object(socket, 'socket', return_value=sock), patch.object(ssl.SSLContext, 'wrap_socket', lambda self, raw, **kw: raw):
        with pytest.raises(safe_egress.EgressError):
            safe_egress.fetch('https://93.184.216.34/a', max_bytes=5, max_redirects=0)
    assert sock.closed


def test_rebinding_on_same_origin_redirect_is_rejected():
    from broker import safe_egress
    sock = WireSocket(b'HTTP/1.1 302 Found\r\nLocation: /b\r\n\r\n')
    answers = [[(socket.AF_INET, 1, 6, '', (ip, 443))] for ip in ['93.184.216.34', '127.0.0.1']]
    with patch.object(socket, 'getaddrinfo', side_effect=answers) as dns, patch.object(socket, 'socket', return_value=sock) as connect, patch.object(ssl.SSLContext, 'wrap_socket', lambda self, raw, **kw: raw):
        with pytest.raises(safe_egress.EgressError):
            safe_egress.fetch('https://docs.example/a')
        assert dns.call_count == 2
        assert connect.call_count == 1


def test_tls_validation_failure_closes_socket():
    from broker import safe_egress
    sock = WireSocket(b'')
    with patch.object(socket, 'socket', return_value=sock), patch.object(ssl.SSLContext, 'wrap_socket', side_effect=ssl.SSLCertVerificationError('mismatch')):
        with pytest.raises(ssl.SSLCertVerificationError):
            safe_egress.fetch('https://93.184.216.34/a')
    assert sock.closed


def test_dns_deadline_does_not_connect():
    from broker import safe_egress
    import threading
    release = threading.Event()
    def stalled(*args, **kwargs):
        release.wait(1)
        return [(socket.AF_INET, 1, 6, '', ('93.184.216.34', 443))]
    try:
        with patch.object(socket, 'getaddrinfo', side_effect=stalled), patch.object(socket, 'socket') as connect:
            with pytest.raises(TimeoutError):
                safe_egress.fetch('https://docs.example/a', timeout=0.01)
            connect.assert_not_called()
    finally:
        release.set()


def test_slow_drip_checks_absolute_deadline():
    from broker import safe_egress
    now = [0.0]
    class DripSocket(WireSocket):
        def recv_into(self, buffer):
            now[0] += 0.6
            return super().recv_into(memoryview(buffer)[:1])
    sock = DripSocket(b'HTTP/1.1 200 OK\r\n\r\nx')
    with patch.object(safe_egress.time, 'monotonic', side_effect=lambda: now[0]), patch.object(socket, 'socket', return_value=sock), patch.object(ssl.SSLContext, 'wrap_socket', lambda self, raw, **kw: raw):
        with pytest.raises(TimeoutError):
            safe_egress.fetch('https://93.184.216.34/a', timeout=1)
    assert sock.closed


def test_malformed_http_is_a_recordable_acquisition_failure(tmp_path):
    sock = WireSocket(b'not http\r\n')
    controller = AcquisitionController('test', tmp_path, {
        'docs': {'kind': 'index', 'url': 'https://93.184.216.34/a',
                 'boundary': 'https://93.184.216.34/'}})
    with patch.object(socket, 'socket', return_value=sock), patch.object(ssl.SSLContext, 'wrap_socket', lambda self, raw, **kw: raw):
        receipt = controller.acquire('docs')
    assert receipt['status'] == 'blocked'
    assert receipt['attempts'][0]['status'] == 'failed'
    assert 'error' in receipt['attempts'][0]
