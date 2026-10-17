# Librarian

Reasoning-driven documentation acquisition & ingestion agent. When an agent meets an
unfamiliar dependency, the complete first-party documentation for the exact installed
version is already in its Hindsight bank before it acts — so it queries memory instead
of improvising. Governed by the locked pair in `docs/` (design framework + development
roadmap); those are reference copies — never edited here.

## Layout

| Path | What it is |
|---|---|
| `src/librarian/launch/` | `librarian-launch.py` — the run supervisor / launcher entrypoint. Imports `broker/` (16-of-26 transitive closure). |
| `src/librarian/ingest/` | `librarian-ingest.py` — ingestion helper: validates, dedupes by durable hash, submits to the bound bank, resumes. |
| `src/librarian/verify/` | `librarian-acquisition-verify.py` — post-write verification against bank operation records. |
| `src/librarian/runtime/` | Docker image build + staged/e2e/live-trial acceptance harness + `acceptance-evidence/`. |
| `src/librarian/rehearsal/` | connected / normal / phase4-readiness / dev-interruption / smoke rehearsal scripts. |
| `broker/` | **Live dependency**, not retired: 26 modules + 43 tests (launch imports `broker.tag_scheme` etc.). Framework §8 lists *which* subsystems to retire only after the migration is accepted — preserve, do not delete. |
| `tools/` | Installable host tools — the installer places these onto the host/profile. |
| `profile/` | Versioned reference copies of the profile assets (`SOUL.md`, `OPERATING_PROTOCOL.md`, `config.yaml`). Live ones live at `~/.hermes/profiles/librarian/`. |
| `archive/scaffolding/` | One-off dev/diagnostic tooling built along the way (`ingestion-scripts/` corpus jobs, phase4/validate/append/extract one-offs, retired `librarian-broker.py`). |
| `archive/output-products/` | Artifacts of past runs (8 broadcom task files, openai/infisical task files, 5 smoke specs, runbooks, `MIGRATION.json`). |
| `pytest.ini` | **Required.** Pins `pythonpath=~/.hermes/hermes-agent` so broker tests can import the repo-root `hermes_yaml` module (editable install doesn't expose it). Without it 14 assertions silently skip. |

## Install

See `docs/` and the installer (to be built). Installing places `tools/` correctly and binds
the profile at `~/.hermes/profiles/librarian/`.

## Test

```
cd Projects/librarian && python -m pytest broker -q
```
Broker tests need a `tools/` sibling dir and the `pytest.ini` pythonpath pin.
