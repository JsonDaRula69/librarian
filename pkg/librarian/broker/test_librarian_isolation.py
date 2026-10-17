import unittest
import os
from unittest import mock

from broker import librarian_isolation as isolation


class IsolationTests(unittest.TestCase):
    def test_run_never_launches_after_preflight_failure(self):
        with mock.patch.object(isolation, 'preflight', side_effect=isolation.IsolationUnavailable('blocked')), mock.patch.object(isolation.subprocess, 'Popen') as launch:
            with self.assertRaises(isolation.IsolationUnavailable):
                isolation.run_isolated(isolation.IsolationConfig(image='sha256:' + 'a' * 64), ['/bin/true'])
            launch.assert_not_called()

    @unittest.skipUnless(os.environ.get('LIBRARIAN_CANARY_IMAGE'), 'explicit local canary image required')
    def test_live_write_process_and_stdio_canary(self):
        config = isolation.IsolationConfig(image=os.environ['LIBRARIAN_CANARY_IMAGE'])
        script = r'''
import os, pathlib, subprocess, sys, socket
assert os.getuid() == 65534
assert sys.stdin.buffer.read() == b'request'
p = pathlib.Path('/scratch/ok'); p.write_text('ok'); assert p.read_text() == 'ok'
for target in ['/etc/canary', '/opt/canary', '/tmp/canary']:
    try: pathlib.Path(target).write_text('bad')
    except OSError: pass
    else: raise AssertionError(target)
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/home/jsondarula/Liv-Home').exists()
assert len([p for p in pathlib.Path('/proc').iterdir() if p.name.isdigit()]) < 10
status = pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status
assert 'Seccomp:\t2' in status
assert 'CapEff:\t0000000000000000' in status
assert set(os.listdir('/sys/class/net')) == {'lo'}
assert subprocess.run([sys.executable, '-c', 'raise SystemExit(7)']).returncode == 7
print('canary-ok')
'''
        result = isolation.run_isolated(config, ['/usr/local/bin/python', '-c', script], request=b'request', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), b'canary-ok')
        result = isolation.run_isolated(config, ['/usr/local/bin/python', '-c', 'raise SystemExit(7)'])
        self.assertEqual(result.returncode, 7)
        with self.assertRaisesRegex(isolation.IsolationUnavailable, 'timeout'):
            isolation.run_isolated(config, ['/usr/local/bin/python', '-c', 'import time; time.sleep(30)'], timeout=1)
        with self.assertRaisesRegex(isolation.IsolationUnavailable, 'output limit'):
            isolation.run_isolated(config, ['/usr/local/bin/python', '-c', "print('x'*100000)"], max_output=1024)

    def test_preflight_fails_closed_without_docker(self):
        with mock.patch.object(isolation, '_docker_json', side_effect=OSError('missing')):
            with self.assertRaises(isolation.IsolationUnavailable):
                isolation.preflight(isolation.IsolationConfig(image='sha256:' + 'a' * 64))

    def test_preflight_rejects_image_volumes(self):
        info = {'OSType': 'linux', 'SecurityOptions': ['name=seccomp,profile=builtin', 'name=apparmor']}
        with mock.patch.object(isolation, '_docker_json', side_effect=[info, [{'Config': {'Volumes': {'/data': {}}}}]]):
            with self.assertRaisesRegex(isolation.IsolationUnavailable, 'VOLUME'):
                isolation.preflight(isolation.IsolationConfig(image='sha256:' + 'a' * 64))

    def test_rejects_mutable_image_and_option_injection(self):
        for image in ['image:latest', '', '--privileged']:
            with self.assertRaises(ValueError):
                isolation.build_command(isolation.IsolationConfig(image=image), ['/bin/true'], name='librarian-test')
        config = isolation.IsolationConfig(image='sha256:' + 'a' * 64)
        for argv in [[], ['--help'], ['sh'], ['/bin/true', '\x00']]:
            with self.assertRaises(ValueError):
                isolation.build_command(config, argv, name='librarian-test')
        with self.assertRaises(ValueError):
            isolation.build_command(config, ['/bin/true'], name='unowned')
        with self.assertRaises(ValueError):
            isolation.build_command(isolation.IsolationConfig(image=config.image, scratch_mb=0), ['/bin/true'], name='librarian-test')

    def test_whole_process_command_has_no_host_access(self):
        config = isolation.IsolationConfig(image='sha256:' + 'a' * 64)
        command = isolation.build_command(config, ['/opt/hermes/.venv/bin/hermes', 'chat', '-q', 'test'], name='librarian-test')
        self.assertIn('--read-only', command)
        self.assertIn('--network=none', command)
        self.assertIn('--cap-drop=ALL', command)
        self.assertIn('--security-opt=no-new-privileges=true', command)
        self.assertIn('--user=65534:65534', command)
        self.assertIn('--entrypoint=/usr/bin/env', command)
        self.assertNotIn('--privileged', command)
        self.assertFalse(any(x.startswith(('--volume', '--mount', '--pid=host')) for x in command))
        self.assertEqual(command[-4:], ['/opt/hermes/.venv/bin/hermes', 'chat', '-q', 'test'])
        self.assertIn('-i', command[command.index(config.image) + 1:])


if __name__ == '__main__':
    unittest.main()
