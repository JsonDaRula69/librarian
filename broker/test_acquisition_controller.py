import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path


class ControllerTests(unittest.TestCase):
    def test_unsafe_paths_and_urls_fail_closed(self):
        from broker.acquisition_controller import AcquisitionController, BoundaryError, safe_path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'link').symlink_to(root, target_is_directory=True)
            for value in ['../escape', '/etc/passwd', 'a/../b', 'link/file', 'a\\b']:
                with self.subTest(value=value), self.assertRaises(BoundaryError):
                    safe_path(root, value)
            controller = AcquisitionController('run', root / 'corpus', {})
            for url in ['https://example.org/docs/../secret', 'https://example.org/docs/%2e%2e/secret',
                        'https://example.org/docs2/page', 'file:///etc/passwd', 'https://user@example.org/docs/a']:
                with self.subTest(url=url), self.assertRaises(BoundaryError):
                    controller._url(url, 'https://example.org/docs/')

    def test_partial_acquisition_and_symlink_tree_are_not_complete(self):
        from broker.acquisition_controller import AcquisitionController
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'a.txt').write_text('real')
            (repo / 'z-link').symlink_to('/etc/passwd')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=T', '-c', 'user.email=t@example.org', 'commit', '-qm', 'fixture'], check=True)
            pin = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()
            plan = {'kernel': {'kind': 'git', 'repository': str(repo), 'commit': pin}}
            controller = AcquisitionController('run', root / 'corpus', plan)
            self.assertEqual(controller.acquire('kernel')['status'], 'partial')
            plan['kernel']['paths'] = ['a.txt']
            controller = AcquisitionController('run2', root / 'corpus2', plan)
            self.assertEqual(controller.acquire('kernel')['status'], 'blocked')

    def test_real_local_index_fetch_and_boundary(self):
        # In-memory wire injection exercises the real parser/controller without
        # running a service or adding a production loopback exception.
        from broker.acquisition_controller import AcquisitionController
        from broker.test_safe_egress import WireSocket
        from unittest.mock import patch
        import socket
        import ssl
        def wire(body):
            return WireSocket(b'HTTP/1.1 200 OK\r\nContent-Length: ' +
                              str(len(body)).encode() + b'\r\n\r\n' + body)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            url = 'https://docs.example/llms.txt'
            controller = AcquisitionController('run', root / 'corpus', {
                'docs': {'kind': 'index', 'url': url, 'boundary': 'https://docs.example/'}
            })
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
            with patch.object(socket, 'getaddrinfo', return_value=answers), patch.object(
                    socket, 'socket', side_effect=[wire(b'# Docs\n- [Guide](guide.md)\n'),
                                                  wire(b'# Real fixture'),
                                                  wire(b'- [Escape](https://example.com/private)')]), patch.object(
                    ssl.SSLContext, 'wrap_socket', lambda self, raw, **kw: raw):
                receipt = controller.acquire('docs')
                self.assertEqual(receipt['status'], 'complete', receipt)
                self.assertEqual(len(receipt['occurrences']), 1)
                self.assertEqual(len(receipt['attempts']), 2)
                self.assertEqual(receipt['attempts'][0]['http_status'], 200)
                bad = AcquisitionController('bad', root / 'bad', controller.plan)
                receipt = bad.acquire('docs')
                self.assertEqual(receipt['status'], 'blocked')
                self.assertIn('boundary', receipt['error'])

    def test_real_pinned_tree_acquisition(self):
        from broker import acquisition_controller as ac
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'Documentation').mkdir()
            for i in range(8):
                (repo / 'Documentation' / f'{i}.rst').write_text(f'document {i}')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-qm', 'fixture'], check=True)
            pin = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()
            controller = ac.AcquisitionController('run', root / 'corpus', {
                'kernel': {'kind': 'git', 'repository': str(repo), 'commit': pin}
            })
            receipt = controller.acquire('kernel')
            self.assertEqual(receipt['status'], 'complete')
            self.assertEqual(len(receipt['occurrences']), 8)
            for row in receipt['occurrences']:
                data = (root / 'corpus' / row['source_path']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), row['sha256'])
                self.assertTrue(row['blob'])
            self.assertTrue((root / 'corpus' / receipt['index_path']).is_file())
            receipt['occurrences'].clear()
            self.assertEqual(len(controller.evidence().snapshot()['sources']['kernel']['occurrences']), 8)


if __name__ == '__main__':
    unittest.main()
