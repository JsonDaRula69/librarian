"""Opt-in Linux/main-thread duplex runner; no launcher or service changes.

The trusted argv must select an audited in-image RPC client. Its first stdin
line is the closed-schema acquisition request, then RelaySession replies. Stdout
is RPC only; stderr is untrusted diagnostic bytes. Never put caller purpose in
argv or request. This is not a generic Hermes CLI/provider adapter.

SIGALRM is exclusively owned during execution, including synchronous controller
callbacks. Adapters must still bound their subprocess/network work themselves;
Python cancellation cannot undo an already committed external side effect.
"""
from dataclasses import dataclass
import json
import math
import os
import selectors
import signal
import subprocess
import threading
import time
import uuid

from . import librarian_isolation as isolation
from .librarian_relay import RelayError, serve_connection
from .librarian_request import parse_request


@dataclass(frozen=True)
class RuntimeResult:
    returncode: int
    stderr: bytes
    container_name: str


class _Watchdog(BaseException):
    # Relay dispatch catches Exception to sanitize adapter errors; deadlines
    # must escape that handler rather than permit another privileged operation.
    pass


def _cleanup(proc, name):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)
    deadline = time.monotonic() + 5
    absent_since = None
    while True:
        subprocess.run(isolation._DOCKER + ['rm', '--force', name],
                       env=isolation._HOST_ENV, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, close_fds=True)
        remaining = subprocess.run(
            isolation._DOCKER + ['ps', '-aq', '--filter', 'name=^/' + name + '$'],
            env=isolation._HOST_ENV, capture_output=True, check=True,
            timeout=15, close_fds=True)
        # Docker may finish an in-flight create after its client is killed.
        # Require a quiet interval, retrying removal of late-created containers.
        now = time.monotonic()
        if not remaining.stdout.strip():
            if absent_since is None:
                absent_since = now
            if now - absent_since >= 1:
                return
        else:
            absent_since = None
        if now >= deadline:
            raise isolation.IsolationUnavailable('sandbox cleanup failed: ' + name)
        time.sleep(.05)


def run_relay(config, argv, session, *, request, timeout=300, max_stderr=65536):
    """Return RuntimeResult after verified removal; limit/protocol errors raise.

    request is a librarian_request schema-1 dict (not arbitrary prose/bytes).
    session is consumed and closed once execution starts; use a fresh session per run.
    Requires main thread and no active real-time interval timer. No host fallback.
    """
    initial = (json.dumps(parse_request(json.dumps(request)), separators=(',', ':')) + '\n').encode()
    if len(initial) > 1048576:
        raise ValueError('request byte limit exceeded')
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 86400 or type(max_stderr) is not int
            or not 0 < max_stderr <= 16777216):
        raise ValueError('invalid runtime limits')
    if threading.current_thread() is not threading.main_thread() or any(signal.getitimer(signal.ITIMER_REAL)):
        raise ValueError('runtime requires main thread and exclusive SIGALRM timer')
    if session.closed:
        raise ValueError('fresh open relay session required')
    name = 'librarian-' + uuid.uuid4().hex
    command = isolation.build_command(config, argv, name=name)
    isolation.preflight(config)
    proc = None
    drainer = None
    stop = threading.Event()
    overflow = threading.Event()
    drain_error = threading.Event()
    diagnostics = bytearray()
    old_handler = signal.getsignal(signal.SIGALRM)
    def alarm(signum, frame):
        if overflow.is_set() or drain_error.is_set() or time.monotonic() >= deadline:
            raise _Watchdog()
    signal.signal(signal.SIGALRM, alarm)
    deadline = time.monotonic() + timeout
    signal.setitimer(signal.ITIMER_REAL, min(.05, timeout), .05)
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=isolation._HOST_ENV,
                                close_fds=True, start_new_session=True, bufsize=0)
        def drain():
            try:
                with selectors.DefaultSelector() as selector:
                    os.set_blocking(proc.stderr.fileno(), False)
                    selector.register(proc.stderr, selectors.EVENT_READ)
                    while not stop.is_set():
                        for key, _ in selector.select(.05):
                            data = os.read(key.fd, 4096)
                            if not data:
                                return
                            capacity = max_stderr - len(diagnostics)
                            diagnostics.extend(data[:capacity])
                            if len(data) > capacity:
                                overflow.set()
                                return
            except Exception:
                drain_error.set()
        drainer = threading.Thread(target=drain, name='librarian-stderr')
        drainer.start()
        os.set_blocking(proc.stdin.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdin, selectors.EVENT_WRITE)
            view = memoryview(initial)
            while view:
                for key, _ in selector.select(max(0, deadline - time.monotonic())):
                    view = view[os.write(key.fd, view[:4096]):]
        serve_connection(session, proc.stdout, proc.stdin,
                         timeout=max(.001, deadline - time.monotonic()))
        proc.stdin.close()
        code = proc.wait(timeout=max(.001, deadline - time.monotonic()))
        drainer.join(timeout=max(.001, deadline - time.monotonic()))
        if drainer.is_alive():
            raise isolation.IsolationUnavailable('stderr drain timeout')
        if overflow.is_set() or drain_error.is_set():
            raise _Watchdog()
        if code == 125:
            raise isolation.IsolationUnavailable('Docker refused sandbox')
        return RuntimeResult(code, bytes(diagnostics), name)
    except _Watchdog:
        reason = 'stderr limit exceeded' if overflow.is_set() else 'stderr drain failed' if drain_error.is_set() else 'sandbox timeout'
        raise isolation.IsolationUnavailable(reason) from None
    except (OSError, subprocess.SubprocessError, RelayError) as exc:
        raise isolation.IsolationUnavailable('sandbox relay failed') from exc
    finally:
        # Stop signal-producing thread before restoring the caller's handler.
        signal.setitimer(signal.ITIMER_REAL, 0)
        stop.set()
        if drainer is not None:
            drainer.join(timeout=1)
        signal.signal(signal.SIGALRM, old_handler)
        session.closed = True
        if proc is not None:
            try:
                _cleanup(proc, name)
            except (OSError, subprocess.SubprocessError) as exc:
                raise isolation.IsolationUnavailable('cannot verify sandbox cleanup: ' + name) from exc
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    stream.close()
