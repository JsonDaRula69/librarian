"""Whole-agent Docker adapter; never falls back to a host process.

Integration: run_isolated(config, absolute_hermes_argv, request=bytes).
Only stdin/stdout/stderr cross the boundary. Treat output as untrusted proposals,
not controller evidence or instructions. A controller must validate bounded RPC
messages; this adapter does not implement that protocol or an inference proxy.
Network is disabled, so online Hermes requires a separately reviewed stdio relay
(or an in-image local model). No host directory, credential, or socket is mounted.

Deployment prerequisites: Linux Docker daemon on /var/run/docker.sock with
working default seccomp/AppArmor and cgroups; trusted controller-only Docker
access; audited local immutable image containing Hermes + all dependencies and
/usr/bin/env, no image VOLUMEs or baked credentials/controller data; non-root
65534 must be able to execute runtime files. All mutable state is ephemeral
/scratch (plus Docker's private /dev and /dev/shm). Nothing is installed/pulled.
The image root is read-only. Kernel/runtime escapes and host administrators are
outside this boundary; same-UID host processes must not be attacker-controlled.
Do not expose this module, Docker access, or controller environment to the agent.

Basis: https://hermes-agent.nousresearch.com/docs/user-guide/security and
https://hermes-agent.nousresearch.com/docs/user-guide/docker; installed Hermes
SECURITY.md 'Whole-process wrapping'. No existing launcher/profile is changed.
"""
from dataclasses import dataclass
import re
import json
import os
import selectors
import subprocess
import time
import uuid
from typing import BinaryIO, cast


_DOCKER = ['/usr/bin/docker', '--host=unix:///var/run/docker.sock']
_HOST_ENV = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'DOCKER_CONFIG': '/nonexistent'}


def _docker_json(args):
    result = subprocess.run(_DOCKER + args, env=_HOST_ENV, close_fds=True,
                            capture_output=True, check=True, timeout=15)
    return json.loads(result.stdout)


def preflight(config: 'IsolationConfig') -> None:
    """Check daemon and image; this is not a substitute for deployment canaries."""
    build_command(config, ['/usr/bin/env'], name='librarian-preflight')
    try:
        info = _docker_json(['info', '--format', '{{json .}}'])
        options = info.get('SecurityOptions', [])
        if info.get('OSType') != 'linux' or not any(
            'name=seccomp,profile=builtin' == item for item in options
        ) or 'name=apparmor' not in options:
            raise IsolationUnavailable('Linux, builtin seccomp and AppArmor required')
        image = _docker_json(['image', 'inspect', config.image])[0]
        if image['Config'].get('Volumes'):
            raise IsolationUnavailable('image VOLUME declarations are forbidden')
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise IsolationUnavailable('Docker preflight failed: ' + str(exc)) from exc


class IsolationUnavailable(RuntimeError):
    """Isolation cannot be established; caller must reject the run."""


@dataclass(frozen=True)
class IsolationConfig:
    image: str
    scratch_mb: int = 256
    memory_mb: int = 1024
    pids_limit: int = 128


def run_isolated(config: IsolationConfig, argv: list[str], *, request: bytes = b'',
                 timeout: float = 300, max_output: int = 1024 * 1024) -> subprocess.CompletedProcess:
    """Bounded one-shot pipe boundary; cleanup includes all container descendants.

    Nonzero agent exits are returned, infrastructure/limit failures raise. No
    interactive host FDs, environment, bind mounts or shell parsing are inherited.
    """
    if not isinstance(request, bytes) or len(request) > 1024 * 1024:
        raise ValueError('request must be bytes of at most 1 MiB')
    if not 0 < timeout <= 86400 or not 0 < max_output <= 16 * 1024 * 1024:
        raise ValueError('invalid timeout/output bound')
    name = 'librarian-' + uuid.uuid4().hex
    command = build_command(config, argv, name=name)
    preflight(config)
    proc = None
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=_HOST_ENV, close_fds=True,
                                start_new_session=True)
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        output = {proc.stdout: bytearray(), proc.stderr: bytearray()}
        deadline = time.monotonic() + timeout
        sent = 0
        total = 0
        with selectors.DefaultSelector() as selector:
            for stream in output:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            if request:
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE)
            else:
                proc.stdin.close()
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise IsolationUnavailable('sandbox timeout')
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    stream = cast(BinaryIO, key.fileobj)
                    if stream is proc.stdin:
                        try:
                            sent += os.write(stream.fileno(), request[sent:sent + 4096])
                        except BrokenPipeError:
                            sent = len(request)
                        if sent == len(request):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        data = os.read(stream.fileno(), 65536)
                        if not data:
                            selector.unregister(stream)
                        else:
                            total += len(data)
                            if total > max_output:
                                raise IsolationUnavailable('sandbox output limit exceeded')
                            output[stream].extend(data)
        code = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        if code == 125:
            raise IsolationUnavailable('Docker refused sandbox: ' + bytes(output[proc.stderr]).decode(errors='replace'))
        return subprocess.CompletedProcess(command, code, bytes(output[proc.stdout]), bytes(output[proc.stderr]))
    except (OSError, subprocess.SubprocessError) as exc:
        raise IsolationUnavailable('sandbox execution failed: ' + str(exc)) from exc
    finally:
        # Killing only the Docker client leaves the agent running in the daemon.
        if proc is not None:
            try:
                subprocess.run(_DOCKER + ['rm', '--force', name], env=_HOST_ENV,
                               capture_output=True, timeout=15, close_fds=True)
                cleanup_deadline = time.monotonic() + 5
                while True:
                    remaining = subprocess.run(
                        _DOCKER + ['ps', '-aq', '--filter', 'name=^/' + name + '$'],
                        env=_HOST_ENV, capture_output=True, check=True, timeout=15,
                        close_fds=True,
                    )
                    if not remaining.stdout.strip():
                        break
                    if time.monotonic() >= cleanup_deadline:
                        raise IsolationUnavailable('sandbox cleanup failed: ' + name)
                    # --rm may still be finishing removal after rm --force returns.
                    time.sleep(0.05)
            except (OSError, subprocess.SubprocessError) as exc:
                raise IsolationUnavailable('cannot verify sandbox cleanup: ' + name) from exc
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    stream.close()


def build_command(config: IsolationConfig, argv: list[str], *, name: str) -> list[str]:
    """Return fixed-policy argv, never shell text or caller-supplied Docker flags."""
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', config.image):
        raise ValueError('image must be a local immutable sha256 image ID')
    if not re.fullmatch(r'librarian-[a-z0-9-]{1,64}', name):
        raise ValueError('invalid container name')
    if not argv or not argv[0].startswith('/') or any(
        not isinstance(arg, str) or '\x00' in arg for arg in argv
    ):
        raise ValueError('absolute executable and NUL-free argv required')
    for value in (config.scratch_mb, config.memory_mb, config.pids_limit):
        if type(value) is not int or not 1 <= value <= 65536:
            raise ValueError('resource limits must be integers in 1..65536')
    return [
        '/usr/bin/docker', '--host=unix:///var/run/docker.sock', 'run', '--rm',
        '--pull=never', '--name=' + name, '--interactive', '--read-only',
        '--network=none', '--ipc=private', '--cgroupns=private',
        '--cap-drop=ALL', '--security-opt=no-new-privileges=true',
        '--user=65534:65534', '--pids-limit=' + str(config.pids_limit),
        '--memory=' + str(config.memory_mb) + 'm',
        '--memory-swap=' + str(config.memory_mb) + 'm', '--cpus=1',
        '--ulimit=nofile=256:256', '--ulimit=core=0:0', '--log-driver=none',
        '--tmpfs=/scratch:rw,nosuid,nodev,noexec,size=' + str(config.scratch_mb)
        + 'm,mode=0700,uid=65534,gid=65534',
        '--workdir=/scratch', '--entrypoint=/usr/bin/env', config.image,
        '-i', 'PATH=/usr/local/bin:/usr/bin:/bin', 'HOME=/scratch',
        'HERMES_HOME=/scratch/hermes', 'TMPDIR=/scratch',
        'PYTHONDONTWRITEBYTECODE=1', *argv,
    ]
