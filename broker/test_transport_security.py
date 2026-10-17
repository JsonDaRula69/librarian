"""Offline security regressions for the credential-bearing HTTP transport."""
import os
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from broker.proxy import UpstreamRequest
from broker.transport import HindsightHTTPTransport, UpstreamTransportError


@contextmanager
def local_server(status=200, body=b'ok', location=None, framing='length'):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get('Authorization')))
            self.send_response(status)
            if location:
                self.send_header('Location', location)
            if framing == 'length':
                self.send_header('Content-Length', str(len(body)))
            elif framing == 'chunked':
                self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            if framing == 'chunked':
                self.wfile.write(f'{len(body):x}\r\n'.encode() + body + b'\r\n0\r\n\r\n')
            else:
                self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class RoutingSecurityTests(unittest.TestCase):
    def test_cross_origin_redirect_never_forwards_authorization(self):
        with local_server() as (target, target_seen):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(status=status), local_server(status, location=target + '/stolen') as (base, seen):
                    response = HindsightHTTPTransport(base, 'test-secret')(UpstreamRequest('GET', '/redirect'))
                    self.assertEqual(target_seen, [])
                    self.assertEqual(response.status, status)
                    self.assertEqual(seen, [('/redirect', 'Bearer test-secret')])

    def test_ambient_proxy_never_receives_credentials(self):
        with local_server(body=b'proxy') as (proxy, proxy_seen), local_server() as (base, seen):
            env = {key: proxy for key in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')}
            env.update(no_proxy='', NO_PROXY='')
            with patch.dict(os.environ, env):
                response = HindsightHTTPTransport(base, 'test-secret')(UpstreamRequest('GET', '/safe'))
            self.assertEqual(proxy_seen, [])
            self.assertEqual(response.body, b'ok')
            self.assertEqual(seen, [('/safe', 'Bearer test-secret')])


class ResponseLimitTests(unittest.TestCase):
    def test_oversized_success_and_error_bodies_are_rejected(self):
        for status in (200, 403, 500):
            with self.subTest(status=status), local_server(status, b'x' * 65) as (base, _):
                with patch('broker.transport.MAX_RESPONSE_BYTES', 64, create=True):
                    with self.assertRaises(UpstreamTransportError) as caught:
                        HindsightHTTPTransport(base, 'test-secret')(UpstreamRequest('GET', '/large'))
                self.assertNotIn('test-secret', str(caught.exception))

    def test_read_limits_do_not_depend_on_content_length(self):
        for status in (200, 403):
            for framing in ('chunked', 'close'):
                with self.subTest(status=status, framing=framing):
                    with local_server(status, b'x' * 65, framing=framing) as (base, _):
                        with patch('broker.transport.MAX_RESPONSE_BYTES', 64):
                            with self.assertRaises(UpstreamTransportError):
                                HindsightHTTPTransport(base, 'test-secret')(UpstreamRequest('GET', '/large'))

    def test_body_exactly_at_limit_is_preserved(self):
        for status in (200, 403):
            with self.subTest(status=status), local_server(status, b'x' * 64) as (base, _):
                with patch('broker.transport.MAX_RESPONSE_BYTES', 64, create=True):
                    response = HindsightHTTPTransport(base, 'test-secret')(UpstreamRequest('GET', '/body'))
                self.assertEqual((response.status, response.body), (status, b'x' * 64))


class PathSecurityTests(unittest.TestCase):
    def test_rejects_paths_that_can_escape_or_be_reinterpreted(self):
        with local_server() as (base, seen):
            transport = HindsightHTTPTransport(base, 'test-secret')
            for path in ('', 'relative', '//evil.invalid/x', 'http://evil.invalid/x',
                         '/x?query', '/x#fragment', '/a/../b', '/a/./b',
                         '/a/%2e%2e/b', '/a/%252e%252e/b', '/a%2fb',
                         '/a\\b', '/a/%5cb', '/a//b', '/x\n', '/x%0d%0a', '/bad%zz'):
                with self.subTest(path=path), self.assertRaises(UpstreamTransportError):
                    transport(UpstreamRequest('GET', path))
            self.assertEqual(seen, [])

    def test_allows_only_document_listing_pagination_query(self):
        with local_server() as (base, seen):
            transport = HindsightHTTPTransport(base, 'test-secret')
            response = transport(UpstreamRequest('GET', '/v1/default/banks/Liv/documents?limit=100&offset=0'))
            self.assertEqual(response.status, 200)
            self.assertEqual(seen, [('/v1/default/banks/Liv/documents?limit=100&offset=0', 'Bearer test-secret')])

    def test_rejects_ambiguous_or_unsafe_queries(self):
        with local_server() as (base, seen):
            transport = HindsightHTTPTransport(base, 'test-secret')
            invalid = (
                '/v1/default/banks/Liv/documents?limit=1&limit=2',
                '/v1/default/banks/Liv/documents?limit=1&offset=0&extra=1',
                '/v1/default/banks/Liv/documents?limit=0&offset=0',
                '/v1/default/banks/Liv/documents?limit=101&offset=0',
                '/v1/default/banks/Liv/documents?limit=1&offset=-1',
                '/v1/default/banks/Liv/documents?limit=1&offset=10000001',
                '/v1/default/banks/Liv/documents?limit=1',
                '/v1/default/banks/Liv/documents?limit=1&offset=0#fragment',
                '/v1/default/banks/Liv/documents?limit=1&offset=0%0a',
                '/v1/default/banks/Liv/documents?limit=1%26offset=0',
                '/v1/default/banks/Liv/other?limit=1&offset=0',
            )
            for path in invalid:
                with self.subTest(path=path), self.assertRaises(UpstreamTransportError):
                    transport(UpstreamRequest('GET', path))
            self.assertEqual(seen, [])


class URLSecurityTests(unittest.TestCase):
    def test_rejects_ambiguous_or_non_numeric_loopback_urls(self):
        invalid = [
            'http://127.0.0.1:80@evil.invalid', 'http://localhost:80',
            'http://127.0.0.1:80.evil.invalid', 'http://127.0.0.1:',
            'http://127.0.0.1:0', 'http://127.0.0.1:65536',
            'http://127.0.0.1:80?x', 'http://127.0.0.1:80#x',
            'http://127.0.0.1:80?', 'http://127.0.0.1:80#',
            'http://127.0.0.1:80/prefix', 'http://127.0.0.1:80\n',
            ' http://127.0.0.1:80', 'http://127.1:80',
            'http://2130706433:80', 'http://[::1%lo]:80',
            'http://[::1]:80@evil.invalid', 'http://192.168.1.1:80',
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                HindsightHTTPTransport(url, 'test-secret')

    def test_accepts_explicit_numeric_loopback_origins(self):
        for url in ['http://127.0.0.1:80', 'http://127.0.0.2:1234/', 'http://[::1]:8888']:
            with self.subTest(url=url):
                self.assertEqual(HindsightHTTPTransport(url, 'test-secret').base_url, url.rstrip('/'))
