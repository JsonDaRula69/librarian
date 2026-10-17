"""GitRemote transport security, without external network access."""
import socket
import subprocess
from unittest.mock import Mock

import pytest

from broker.controller_integrations import GitRemote
from broker.acquisition_controller import BoundaryError

PIN = 'a' * 40
URL = 'https://docs.example/project.git'


@pytest.mark.parametrize('fetch_status', [0, 128])
def test_fetch_pins_address_and_discards_ambient_state(monkeypatch, fetch_status):
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:9999')
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_SSL_NO_VERIFY', '1')
    monkeypatch.setenv('LD_PRELOAD', '/evil.so')
    dns = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    runs = []
    def run(args, **kwargs):
        runs.append((args, kwargs))
        data = b'http.curloptResolve\n' if '--config' in args else (PIN + '\n').encode()
        return subprocess.CompletedProcess(args, 0, stdout=data)
    monkeypatch.setattr(subprocess, 'run', run)
    process = Mock()
    process.poll.return_value = fetch_status
    popen = Mock(return_value=process)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    if fetch_status:
        with pytest.raises(BoundaryError, match='fetch failed'):
            with GitRemote().acquire(URL, PIN):
                pytest.fail('failed fetch yielded')
    else:
        with GitRemote().acquire(URL, PIN):
            pass
    assert popen.call_count == 1  # Never retry without the pin.
    args, kwargs = popen.call_args
    command = args[0]
    assert 'http.curloptResolve=docs.example:443:8.8.8.8' in command
    assert URL in command  # TLS authenticates hostname, not the numeric pin.
    assert 'http.sslVerify=true' in command
    assert 'http.followRedirects=false' in command
    assert 'http.proxy=' in command
    assert 'credential.helper=' in command
    assert command[0] == '/usr/bin/git'
    assert dns.call_count == 1
    for _, options in runs + [(command, kwargs)]:
        env = options['env']
        assert not {'HTTPS_PROXY', 'GIT_CONFIG_COUNT', 'GIT_SSL_NO_VERIFY', 'LD_PRELOAD'} & env.keys()
        assert env['GIT_CONFIG_NOSYSTEM'] == '1'
        assert env['GIT_CONFIG_GLOBAL'] == '/dev/null'
        assert options['cwd'] == env['HOME']


@pytest.mark.parametrize('bad', ['127.0.0.1', '10.0.0.1', '169.254.169.254',
                                '224.0.0.1', '::ffff:8.8.8.8', '64:ff9b::808:808'])
def test_any_unsafe_dns_answer_blocks_all_subprocesses(monkeypatch, bad):
    answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
               (socket.AF_INET6 if ':' in bad else socket.AF_INET,
                socket.SOCK_STREAM, 6, '', (bad, 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: answers)
    run, popen = Mock(), Mock()
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    with pytest.raises(BoundaryError):
        with GitRemote().acquire(URL, PIN):
            pytest.fail('unsafe repository yielded')
    run.assert_not_called()
    popen.assert_not_called()


def test_missing_pin_support_fails_before_network(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    monkeypatch.setattr(subprocess, 'run', Mock(return_value=
        subprocess.CompletedProcess([], 0, stdout=b'http.proxy\n')))
    popen = Mock()
    monkeypatch.setattr(subprocess, 'Popen', popen)
    with pytest.raises(BoundaryError, match='support required'):
        with GitRemote().acquire(URL, PIN):
            pytest.fail('unsupported Git yielded')
    popen.assert_not_called()


@pytest.mark.parametrize('error', [socket.gaierror('no DNS'), TimeoutError('DNS timeout')])
def test_dns_failure_never_starts_git(monkeypatch, error):
    monkeypatch.setattr(socket, 'getaddrinfo', Mock(side_effect=error))
    run = Mock()
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(BoundaryError):
        with GitRemote().acquire(URL, PIN):
            pytest.fail('failed DNS yielded')
    run.assert_not_called()


def test_ipv6_pin_is_bracketed(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k:
        [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2606:4700:4700::1111', 443))])
    assert GitRemote()._validated_url(URL) == (URL, 'docs.example:443:[2606:4700:4700::1111]')


@pytest.mark.parametrize('limit', [float('nan'), float('inf')])
def test_nonfinite_deadline_rejected(limit):
    with pytest.raises(BoundaryError):
        GitRemote(timeout=limit)

