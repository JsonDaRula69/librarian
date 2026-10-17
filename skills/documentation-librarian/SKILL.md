---
name: documentation-librarian
description: Use when a dependency lacks complete first-party docs.
version: 1.2.0
author: Teja, Liv
license: MIT
metadata:
  hermes:
    tags: [documentation, ingestion, deduplication, provenance, librarian]
---

# Documentation Librarian Workflow

Use when the caller's own corpus lacks complete current documentation for a product, program, service, API, library, platform, provider, driver, package, or operational dependency.

## Worker Routing

Keep architecture, difficult debugging, and security review in Liv's main Astra session; do not delegate them. Use deterministic scripts for mechanical tasks. The development-worker route is distinct from this static Librarian model: delegate precise implementation/tests only to verified OpenAI routes, Luna low by default, and verify the actual model and reasoning rather than trusting prompt wording. Never retune or restart already-running workers; judge routing from measured quality, cost, elapsed time, tests, rework and escalation.

## Model Route Changes

- The librarian profile's model is production-critical: the launcher spawns `hermes --profile librarian chat`, so `config.yaml` `model.default` governs every run and no launch flag overrides it.
- `chatgpt/*` routes die with an upstream plan limit (`usage_limit_reached`, no failover). Probe the route before blaming the pipeline: a one-token completion through `http://127.0.0.1:4000/v1/chat/completions` distinguishes a dead route from a broken run.
- Switching the route means updating every pin, not just the profile: `broker/librarian_inference.py` `MODEL` plus the literals in `tools/librarian-connected-rehearsal.py`, `tools/librarian-phase4-readiness.py`, `tools/librarian-runtime/{e2e_acceptance,live_trial}.py` and `broker/test_librarian_inference.py`.
- Verify a switch end-to-end with a real one-turn profile session (`hermes --profile librarian chat --query-file <tiny-prompt> --max-turns 2 --run-budget 120`): it exercises the profile → LiteLLM → provider chain, including `agent.reasoning_effort`, which a direct API probe does not.

## Bank Reuse Identity

- Prove reuse from the bank's own listing at the exact prepared SHA-256, scoped to the bound bank, plus a terminal durable claim bound to the same caller, bank and hash. A completed operation ID, a receipt, or a well-formed report is not proof on its own.
- Retained bank documents do not reproduce the corpus `product`/`version`/`source`. Compare those fields only when the bank row actually exposes them, and still refuse the match when an exposed value conflicts. Demanding metadata equality for hash-identical bytes leaves reuse unresolvable forever and strands every already-ingested artifact in reconciliation.
- Never re-post bytes the bank already holds when no durable claim corroborates them: return `unknown` (reconciliation required) rather than `missing`, because a re-post duplicates content.
- Compare SHA-256 digests case-insensitively; an absent or empty digest never matches.
- Expired run leases are never stolen silently. Reconcile one explicitly through the claim store's fenced `release(namespace, product, version, fingerprint, lease_owner, run_id)` after confirming no live process uses that run ID, then verify the claim table is empty.

## Gate Verdicts Are Not Self-Evidently True

- `evidence-gate-failed` / `missing evidence: <name>` is a claim to check, not a fact: confirm the named artifact exists, is non-symlink, and parses (`json.load`), then re-run `tools/librarian-acquisition-verify.py --run-dir <run> --corpus-root <corpus>` yourself before diagnosing acquisition as incomplete. A gate verdict produced before a correction pass finished describes the run that no longer exists.
- A correction session's non-zero exit does not invalidate the artifacts it just wrote — a capped session can repair everything and still exit on its turn cap. Re-evaluate the gate before honouring that exit, and never require the *first* session's exit code as a precondition for prepare; otherwise a repaired run is condemned to `evidence-gate-failed` and the bank-first stage never runs.
- `disposition=unknown` with `bank_coverage.complete=false` means the whole-bank scan did not finish inside its budget, not that the bank lacks the content: raise `--inventory-max-pages` and `--status-deadline-seconds` for one pass long enough to complete rather than reading it as missing.

## Stale Verdicts and Artifact Truth-Repair

- A correction session's writes can land *after* the supervisor's final gate evaluation (its bash work gets promoted to background, so the session returns while the artifact build is still running). The run then contradicts itself: `acquisition-result.json` / `workflow-result.json` carry `partial` + `evidence-gate-failed` while the artifacts they condemn are on disk and valid. Before reporting a run as failed, re-run the gate yourself; the recorded verdict is a claim, not a fact.
- Truth-repair protocol for a run whose artifacts disagree: (1) re-run the real tooling — gate plus `prepare` — and keep the raw outputs in the run dir as receipts (`gate-recheck-N.json`, `prepare-N.stdout/.stderr`); (2) copy every artifact you change to `<name>.pre-correction-N` first; (3) record the repair, the superseded verdict and the rollback paths in `gaps.json` `status_evidence` plus a correction section in `agent-note.md`; (4) re-derive the result JSONs with the launcher's own functions (`run_acquisition_evidence_gate`, `reconcile_acquisition_artifact_paths`, `resolve_component_manifests`, `acquisition_gaps_complete`, `summarize_tag_sets`) so the schema and the `valid`/`status` semantics are the launcher's, not yours.
- Never trust an agent-note's "validated: gate ok / dry-run accepted" line: in one run it cited `scratch/dry-run-*.json` files that had never been written, and the helper genuinely failed. Verify claims by re-running the tool.

## Oversize Artifacts and the Intake Bound

- Preparation refuses any artifact larger than twice the controller's `max_bytes`, and `tools/librarian-ingest.py` hard-codes the controller default of 32 MiB — so **any file above 64 MiB can never be prepared or ingested** through the sanctioned path, whatever the manifest says. Check row sizes in the manifest before promising an ingestible run, and expect a direct/curl fetch to have bypassed that cap silently.
- A platform-wide aggregate index (`https://developers.cloudflare.com/llms-full.txt` at 67.6 MB: whole-platform content, ~1.6k `/r2/` and ~1.7k `/pages/` references) is enumeration evidence, not an occurrence of the declared components. Demote it to a retained, unmanifested evidence asset — bytes and sha256 kept on disk, excluded in `gaps.json` with the measured reason — rather than letting it block the run. Coverage is unchanged when every in-boundary page is separately manifested and the product-level `llms-full.txt` stays in.
- Demoting a row means editing three places in the manifest or the gate fails: `files`, `source_enumeration.enumerated_paths` (set-equality with the file rows), and any `surface_accounting` entry that lists the path as evidence (`covered surface lacks evidence paths` / `references unmanifested evidence`). Splitting an oversize source is not an option — preparation must not change source bytes.

## Isolated Dev Bank (end-to-end testing without touching Liv)

Use `tools/dev-bank/` (see its README) for the whole writable path against a throwaway Hindsight instance on `127.0.0.1:18888` — the endpoint `tools/librarian-ingest.py --dev-instance` expects. `tools/hindsight-dev-restore.py` and `tools/hindsight-dev-api.py` are stale (deleted backup, `network=none` assertion, unwritable `/run/secrets`); `tools/dev-bank/standup.sh --worker --embeddings-fixture` replaces both.

- **A bank must exist before retaining into it.** `POST …/files/retain` returns `404` for a missing bank, and the helper records `BoundaryError`/`unknown` with `polls: 0`. Validation runs first, so a probe with an incomplete body returns `422` and looks healthy — create the bank with `PUT /v1/default/banks/<bank>` (`{}` is valid) before diagnosing anything else.
- A `404` on retain is a **definitive rejection**, not an interruption: no operation exists, so reconcile those ledger claims (receipt status not accepted/completed, no `operation_id`, 4xx `http_status`) before re-submitting. See `tools/dev-bank/reconcile-dev-claims.py`.
- Migrations need `HINDSIGHT_API_TEXT_SEARCH_EXTENSION=native`; `pg_search` is absent from the image and aborts the initial-schema migration with `FeatureNotSupported`.
- **Use one worker.** With `HINDSIGHT_API_WORKERS=4` the embedded Postgres deadlocked on concurrent `batch_retain`, failing 2 of 37 operations and losing the uploaded bytes for one artifact (`File not found: banks/<bank>/files/…`), which then re-fails on retry.
- The fixture's mock LLM plus the deterministic local embedding server (`embedding-fixture.py`) prove pipeline mechanics only: document counts, operation states and recall work, but the "facts" are not extraction evidence and scores are not embedding-quality evidence. Say so in any report.
- Raise `--inventory-max-pages` (e.g. 500) for every dev-bank `status`/`submit`; the default 2 pages returns UNKNOWN and refuses the write once the bank holds content.
- A cached **empty** bank scan is not an absence proof forever. `inventory_checkpoint` used to return a cached `complete, total: 0` result without re-reading the bank, so the first scan of an empty bank froze that verdict: after ingestion every artifact fell to `unknown | matching claim requires reconciliation` (hard no-write) and a genuinely absent one read `missing`. Fixed 2026-10-02 — the empty case now pays the same single freshness read and then either proves absence again or reports `restart_required` and rescans. If reuse still looks broken, check the checkpoint's `total` against the bank's before blaming anything else, and remember a stale result needs a second bounded invocation to converge.
- The retained `content_hash` is the **payload** hash, not the prepared artifact digest — that is normal, not a defect: `_normalise` recovers the prepared SHA-256 from `retain_params.context.artifact_sha256`. Never diagnose a reuse failure from a hash mismatch between a bank row and a manifest row; verify the preflight verdict with a fresh checkpoint instead.
- Identity fields (product/version) recorded only inside the JSON context string are invisible unless parsed. `_context_containers`/`_context_field` now surface them so a conflicting product/version on hash-identical bytes refuses the match; `source` is only taken when the context names it explicitly, so path 1 (`verified`) stays unreachable for such rows and reuse correctly rests on the completed-claim plus exact-bytes path. `HindsightInventoryReader.inventory()` — the advisory discovery-only bank line — still reports incomplete in that shape.

## Retain Strategy

- Every submitted file must name the bank's document-ingestion strategy: `files_metadata[i].strategy = 'Doc Ingestion'` (per-file field of `FileRetainRequest`, a key of the bank's `retain_strategies`). Omit it and the retain silently rides `retain_default_strategy` (`Live Session`), so documents are extracted under the concise live-session profile instead of the verbose document profile. Fixed in `broker/controller_integrations.py` (`DOC_RETAIN_STRATEGY`).
- An unknown strategy name only logs `Unknown retain strategy '<name>', using resolved config as-is` (`config_resolver.apply_strategy`) and keeps the resolved config — naming it can never corrupt a bank that does not declare it, but it also fails silently, so verify rather than assume.
- Two evidence channels prove which profile ran: the operation's `task_payload.strategy` and the recorded prompt in `GET /v1/default/banks/<bank>/llm-requests` under scope `retain_extract_facts` (concise says "Be SELECTIVE"; verbose says "FIVE required dimensions - BE EXTREMELY DETAILED").

## Bounded Status Scans

- `tools/librarian-ingest.py status` is read-only but bounded per invocation by its status deadline; a large bank needs `--status-deadline-seconds` set above the default and `--inventory-max-pages` above the page count.
- The inventory checkpoint restarts its scan whenever the bank's reported total changes, so a bank receiving continuous retains needs one pass long enough to finish inside a single invocation. Sliced rescans of a growing bank never converge.

## Launch Mechanics (verified 2026-10-02)

- **Invoke the launcher through the interpreter that has `openai`**: `/home/jsondarula/.hermes/hermes-agent/venv/bin/python tools/librarian-launch.py …` with `HERMES_HOME` set. The tool scripts are `0644` and import `broker.*` + `openai`, so running `tools/librarian-launch.py` directly fails with `Permission denied`, and the system `python3` lacks the dependency.
- **`--max-turns` is bounded 1..100**; the launcher raises `ValueError: max_turns must be between 1 and 100` (and `inventory_status_attempts` 1..64). `--run-budget` is wall-clock **seconds** for the discovery phase, not turns.
- **A launch that dies *after* taking the claim strands a live lease.** The next attempt is refused with `status=attach-existing`, `errors=["duplicate acquisition claim"]` — not a duplicate-run bug, just an unreleased row. Confirm no live process holds the run id, then release it explicitly with the claim store's fenced `release(namespace, product, version, scope_fingerprint, lease_owner, run_id)` using the exact values from `<corpus_root>/dedupe/live-trial-claims.sqlite` (`broker.corpus_preflight.RunClaimStore`), and verify the table is empty before relaunching.
- `--resume-session <hermes session id>` plus `--run-id <run>` continues one attempt; the launcher moves each attempt's artifacts into `<run>/resume-attempts/<session>/<attempt-hash>/`, so the live copies are the ones at the run-dir root.
- A run that exhausts its turns mid-workflow reports `status=partial` with `prepared_status=evidence-gate-failed` and leaves usable acquisition artifacts (page trees, enumerators, download plans) in the run dir — resume rather than re-acquiring.

## Supervisor Evidence Gate — Exact Contract (verified 2026-10-02)

`tools/librarian-acquisition-verify.py` demands three things beyond file/byte accounting. Learn them before burning correction passes:

- `component-manifest-index.json` → a `completeness_accounting` block with non-empty lists plus a `challenge_path` that resolves to a completeness-challenge file whose own `status` is `"passed"`, whose `run_id` matches, and whose `component_manifest_paths` **set-equals** the index's manifest paths.
- `gaps.json` → `status: "complete"` **and** an empty `gaps` list **and** `semantic_status: "complete"`.
- Failure text names one condition at a time, in the verifier's own order: `missing evidence: …`, then `completeness challenge has not passed`, then `gaps evidence does not establish semantic completeness`. Fix the named one; do not re-run blind.

An honest `outcome: "partial"` with a populated `gaps` array can never pass — exclusion notes (marketing pages out of scope, a live docs set that is not versioned per release, binaries kept as evidence rather than text) belong in `agent-note.md`, and a run that puts them in `gaps.json` fails on bookkeeping while its acquisition is genuinely complete. Check which condition the verifier names before launching another correction pass.

## Run

1. Trigger when complete documentation is missing during normal work or on an explicit user request. Start bank-first: check the caller's Hindsight inventory and corpus/ingestion ledger by product, version, canonical source, document identity and hash. Recall cannot prove absence or completeness; unknown state requires reconciliation, not automatic re-ingestion. Send only software identities, versions and dependency evidence. Never forward purpose, task narrative, selected-page instructions or conversation text.
2. Bind the run claim to the caller profile/agent, absolute caller workspace and corpus root, caller-owned Hindsight bank, requested products/versions, and run ID before launch. Include only separately verified scope exceptions; never infer another caller, path or bank.
3. The supervisor is the single launcher and process owner. Inspect active matching runs and reuse them rather than spawning duplicates. Launch only `/home/jsondarula/Liv-Home/tools/librarian-launch.py` with the strict request file, caller binding, `--foreground`, and bounded budgets. The default production path now performs normal-profile acquisition and offline preparation, then a complete bank-first status gate. It reuses verified-present content and never submits unless the operator explicitly adds `--allow-submit`.
4. Keep each caller's workspace, corpus, ledger, reports and bank physically separate. Do not create a duplicate matching run or access another profile's state. The invoked Librarian must not recursively launch another Librarian or its parent supervisor; the caller may initiate distinct authorized runs through the supervisor.
5. Monitor recent transcript, timestamps, artifacts and test results; liveness alone is not progress. Report concrete blockers and retain run evidence.
6. After completion, use the existing helper at `tools/librarian-ingest.py` for prepared-manifest, ledger and operation-status checks when artifacts exist. Independently verify hashes, occurrence-to-manifest parity, bank binding, operation status, recall readiness, lease revocation and cleanup. Discovery-only success is not acquisition, ingestion or corpus completeness.

The launcher injects `/home/jsondarula/.hermes/profiles/librarian/OPERATING_PROTOCOL.md` as the single Librarian operational authority; consult it for binding, isolation, acquisition, ingestion and verification rules rather than duplicating them here. `SOUL.md` defines identity only. Requests use the launcher's strict JSON schema; unknown fields and legacy prose are rejected. User scope exceptions require separately verified authorization.

Report success only for the phase actually verified. The default launcher path is the supported production workflow: normal-profile acquisition, hardened evidence verification, offline preparation, complete bank-first status, and optional explicitly authorized submission. `--discovery-only` and `--acquire-only` remain diagnostic modes. A valid report or structural gate alone never proves complete acquisition or terminal ingestion; live submission success requires persisted operation IDs, terminal status, scoped recall, and cleanup evidence.
