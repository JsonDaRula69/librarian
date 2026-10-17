# Librarian

Reasoning-driven documentation acquisition & ingestion agent. When an agent meets an
unfamiliar dependency, the complete first-party documentation for the exact installed
version is already in its Hindsight bank before it acts — so it queries memory instead
of improvising. Governed by the locked pair in `docs/`.

Distributed as a **Hermes portable plugin** (`hermes plugins install JsonDaRula69/librarian`).

## Layout

| Path | What it is |
|---|---|
| `plugin.yaml` / `__init__.py` | Plugin manifest + entrypoint (`register(ctx)`; `librarian_provision` tool). |
| `pkg/librarian/` | The agent source package (`broker` is a top-level import inside it). |
| `pkg/librarian/broker/` | Broker/lease/claim runtime — 26 modules + 43 tests. Live dependency of launch. |
| `pkg/librarian/launch/` | `librarian-launch.py` — run supervisor / launcher (spawns `hermes --profile librarian chat`). |
| `pkg/librarian/ingest/` | `librarian-ingest.py` — ingestion helper. |
| `pkg/librarian/verify/` | `librarian-acquisition-verify.py` — post-write verification. |
| `pkg/librarian/runtime/` | Docker build + acceptance harness + `acceptance-evidence/`. |
| `pkg/librarian/rehearsal/` | connected / normal / phase4-readiness / dev-interruption / smoke rehearsals. |
| `pkg/profile/` | Profile assets the provisioner places into `~/.hermes/profiles/librarian/`. |
| `pkg/tools/` | Installable host tools. |
| `skills/` | Bundled caller skill (`documentation-librarian`). |
| `pytest.ini` | Pins `pythonpath=~/.hermes/hermes-agent` so broker tests can import `hermes_yaml`. |
| `archive/` | One-off dev tooling + past-run artifacts (not shipped runtime). |
| `docs/` | Locked design framework + roadmap (reference copies). |

## Install

```
hermes plugins install JsonDaRula69/librarian
hermes plugins enable librarian
```
Then provision the profile: call the `librarian_provision` tool, or
`python -c "import __init__; __init__.provision()"`.

## Develop

Dev work lives here. Test: `python -m pytest pkg/librarian/broker -q` (needs the pytest.ini pin + a tools/ sibling).
