# Immutable Librarian Hermes stdio runtime

Verified image: `sha256:42fd0b04dc2afd98cdbccada2230bcd4a46ee931dc413a8a307126152ae59023`.
`image.json` records the Docker-local immutable ID and input-lock digest.

This is the actual `broker.librarian_agent_client` bounded driver, using real
Hermes `ProviderProfile.create_client`, `ChatCompletionsTransport` and private
`ToolRegistry`. It is NOT the full AIAgent loop: no ambient plugin/config,
memory, compression, auxiliary inference or credential discovery is invoked.
The eight controller tools include source catalog/discovery operations. Model
final text is discarded; exit 0 never establishes ingestion success.

## Build inputs and provenance

`build.py` constructs a temporary context containing ONLY `Dockerfile` and a
normalized `rootfs.tar`. The Dockerfile starts FROM scratch; no donor layers or
configuration are inherited. No downloads, pulls, paid services, production
requests, host mounts, privilege changes or daemon restarts are needed.

- Existing donor image: `sha256:81ea5a19b1133dbad0726bfee4ddcfb66d2fb62dbe61ca2f05e8536074967c8e`.
  Only Python 3.11.15, standard library (excluding tests, bytecode, site-packages,
  Tk/IDLE/ensurepip), `/usr/bin/env`, and dynamic libraries are exported using a
  read-only, non-root, network-none container. No donor home/config is exported.
- Hermes source: exact Git objects from revision
  `922c0d670cf46952248dc22c27e4d01531a4a051`, restricted to the explicit
  `HERMES_FILES` list in `build.py`. No worktree/profile or entire source tree.
- Installed packages: explicit pinned `PACKAGES` list in `build.py`, from the
  Hermes Python 3.11 venv. RECORD hashes are checked; bytecode, installer paths,
  direct-url metadata, executable scripts and .pth hooks are excluded.
- Workspace: ONLY `broker/librarian_agent_client.py` and
  `broker/librarian_request.py`. The image-only `broker/__init__.py` is empty.
  Controller/server modules, broker credentials, corpus, profiles, caches,
  launcher and workspace context files are absent.
- `inputs.lock.json` enumerates all 2,214 destination files, SHA256, size and mode,
  plus source revision, donor ID and exact package versions. Every normal build
  rejects drift. `--refresh-lock` is an explicit review operation, not a fix for
  unexplained drift. The lock was independently re-collected and matched.

Installed package versions: PyYAML 6.0.3, openai 2.24.0, typing_extensions 4.16.0,
tqdm 4.70.0, sniffio 1.3.1, pydantic 2.13.4, typing-inspection 0.4.4,
pydantic_core 2.46.4, annotated-types 0.8.0, jiter 0.16.0, httpx 0.28.1,
idna 3.18, httpcore 1.0.9, h11 0.16.0, certifi 2026.5.20, anyio 4.14.2,
distro 1.9.0. No package installation occurs during the build.

```sh
/home/jsondarula/.hermes/hermes-agent/venv/bin/python tools/librarian-runtime/build.py
/home/jsondarula/.hermes/hermes-agent/venv/bin/python -m pytest tools/librarian-runtime/test_image.py -q
```

The image contains the narrow Hermes import closure, not the full Hermes CLI.
Its Docker-reported size is 28,644,697 bytes; generated context is approximately
81.66 MB. Immutable identity is per build; BuildKit attestations can change the
manifest ID even with identical input files. Pin `image.json`, not a mutable tag.

## Trusted runtime invocation

```python
from broker.librarian_isolation import IsolationConfig
from broker.librarian_runtime import run_relay

result = run_relay(
    IsolationConfig('sha256:42fd0b04dc2afd98cdbccada2230bcd4a46ee931dc413a8a307126152ae59023'),
    ['/usr/local/bin/python', '-I', '-m', 'broker.librarian_agent_client'],
    fresh_session,  # RelaySession with trusted ControllerAdapter and InferenceProxy
    request={'schema_version': 1, 'products': [{'id': 'docs', 'version': 'unknown'}]},
    timeout=30,
)
```

The existing runner supplies network none, read-only root, UID/GID 65534,
cap-drop ALL, no-new-privileges, resource limits, private IPC/cgroup namespace,
and no host mounts. Its env -i supplies HOME=/scratch,
HERMES_HOME=/scratch/hermes, TMPDIR=/scratch and disabled bytecode writes.
Python -I uses in-image site-packages without trusting scratch import paths.
The HOME starts empty on each run; only the private scratch tmpfs is writable
apart from Docker private device/shm filesystems. The image has no VOLUME.
The initial stdin line is the closed schema-1 request; subsequent stdin lines
are RPC replies. Stdout is RPC only; diagnostics go to bounded stderr.

## Live trial invocation

The trial is discovery-only and never enables ingestion or bank writes. The
caller profile is `default`; the target bank remains `Liv`. Inventory reads are
bounded (two pages, no detail reads). An incomplete read is reported as
`unknown`, and discovery may still run read-only; its final result remains
`reconciliation_required`. `--preflight-only` returns `partial` when inventory
is incomplete. Routes, image identity, authority seed, and run-id/path safety
are checked before artifacts or execution. There is no acquisition CLI stage.

```sh
python tools/librarian-runtime/live_trial.py \
  --request-file request.json --authority-seed https://example.invalid \
  --authority-map tools/librarian-runtime/ubuntu-authority-map.json \
  --run-id trial-001 --preflight-only
```

`--authority-map` is optional. When supplied, it must be a strict JSON object
mapping trusted HTTPS origins to arrays of linked HTTPS origins; it is validated
by `EvidenceSourcePolicy` and supplies no prompt fields. If omitted, linked
origins remain denied by default. The checked-in Ubuntu map is operator-approved
public Ubuntu infrastructure test metadata, not a generic endorsement of links.

An existing run may be resumed only with `--recover-run RUN_ID`, and the value
must equal `--run-id`. Recovery requires an expired claim plus a controller
process identity receipt (`controller-process.json`) proving the recorded
process is dead; missing or ambiguous identity is refused. Recovery never
deletes a claim and writes `recovery-receipt.json`. Browser discovery receipts
are controller-written to `discovery-browser-receipts.json` with bounded text,
URLs, and hashes. Older runs without the process identity cannot be safely
recovered and must be left for explicit operator reconciliation.


`test-results.xml`: **4 passed in 11.88s**, actual Docker, no skipped image tests.

- Full in-container file hash inventory matches the lock. Empty broker init and
  exactly the two allowed broker modules verified.
- Live canaries: UID/GID, zero capabilities, seccomp, no-new-privileges, only loopback,
  denied external connection, denied root writes, empty scratch, no host home,
  no Docker socket, no shell, stripped environment; scratch write succeeds.
- Actual module entrypoint via existing run_relay: seven fixture inference
  responses drive catalog, acquire, prepare, submit, poll, discovery status and
  final. Four expected fixture controller/ingestion calls verified, exit 0,
  empty stderr. No inference/model implementation is substituted in the image.
- Unknown tool and non-allowlisted source both terminate nonzero without a
  fixture acquisition call. Controller rejection produces a bounded Hermes
  registry traceback on stderr, never protocol stdout; this is not a secret leak.
- Docker inspection during real client execution verifies no bind/device mounts,
  tmpfs-only mounts, non-root, nonprivileged, cap-drop and network/read-only flags.
  Exact test containers are verified removed after success and rejection.
  Intermediate failed-build images were removed; final image is retained.

Dependency debugging found the lazy chat transport import of
`agent.transports.codex` (prompt-cache helpers) and Rust extension dependencies
libgcc_s/librt/libpthread/libdl. These real dependencies are included unchanged;
there are no stub modules or monkeypatches in the shipped runtime.

This audit establishes the fixture-backed runtime and isolation boundary, not
production controller completion, comprehensive package vulnerability scanning,
or a full-AIAgent integration. Launcher/broker files are untouched. Production
wiring must use the exact argv and pinned image, fresh trusted session, catalog
policy and controller evidence; do not treat model final text as completion.

References inspected before building: local Hermes developer provider/plugin
and programmatic integration documentation (recorded by the preceding client
integration), this README, broker client/request/runtime/isolation/relay modules,
and the actual pinned Hermes provider, transport, registry and import dependencies.
