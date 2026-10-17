from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest

from broker.proxy import UpstreamRequest
from broker.transport import HindsightHTTPTransport, UpstreamTransportError


class MockUpstreamHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, format, *args):
        return

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(length) if length else b''
        type(self).requests.append({
            'method': self.command,
            'path': self.path,
            'authorization': self.headers.get('Authorization'),
            'content_type': self.headers.get('Content-Type'),
            'body': body,
        })
        if self.path == '/error':
            payload = b'{"detail":"no"}'
            self.send_response(403)
        else:
            payload = json.dumps({'path': self.path}).encode()
            self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class TransportTests(unittest.TestCase):
    def setUp(self):
        MockUpstreamHandler.requests = []
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), MockUpstreamHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_injects_static_key_only_upstream(self):
        transport = HindsightHTTPTransport(self.base, 'upstream-static-secret')
        response = transport(UpstreamRequest('POST', '/retain', b'payload', 'multipart/form-data; boundary=x'))
        self.assertEqual(response.status, 200)
        request = MockUpstreamHandler.requests[0]
        self.assertEqual(request['authorization'], 'Bearer upstream-static-secret')
        self.assertEqual(request['content_type'], 'multipart/form-data; boundary=x')
        self.assertEqual(request['body'], b'payload')

    def test_preserves_safe_http_error_response(self):
        transport = HindsightHTTPTransport(self.base, 'upstream-static-secret')
        response = transport(UpstreamRequest('GET', '/error'))
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.body), {'detail': 'no'})

    def test_rejects_non_loopback_and_bad_timeout(self):
        with self.assertRaises(ValueError):
            HindsightHTTPTransport('http://0.0.0.0:8888', 'secret')
        with self.assertRaises(ValueError):
            HindsightHTTPTransport('https://example.com', 'secret')
        with self.assertRaises(ValueError):
            HindsightHTTPTransport(self.base, 'secret', 0)

    def test_network_failure_does_not_leak_secret(self):
        transport = HindsightHTTPTransport('http://127.0.0.1:1', 'do-not-leak', 0.1)
        with self.assertRaises(UpstreamTransportError) as ctx:
            transport(UpstreamRequest('GET', '/health'))
        self.assertNotIn('do-not-leak', str(ctx.exception))
        self.assertNotIn('Authorization', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
