# Librarian Development Roadmap

**Authority:** This is the authoritative execution roadmap for the approved [design framework](2026-09-07_161153-librarian-design-framework.md). The framework governs behavior and architecture; this document governs sequence, tasks and completion evidence. Do not create competing implementation plans.

**Current status:** Framework approved; roadmap aligned. Implementation of the replacement architecture has not been certified. Earlier component tests are reusable evidence, not proof that these phases are complete. This revision changes documentation only.

**Primary milestone:** Ready for bounded live testing at the end of Phase 4. Production rollout is a separate milestone after Phase 5.

## Execution rules

- Follow the framework rather than copying its policies into code prompts or this roadmap. References below identify the governing sections.
- Maintain these checkboxes as the source of truth. The runtime todo list mirrors the current phase; it does not supersede this document.
- Mark a task complete only with a recorded result. Each phase records owner, status, changed paths, verification command/result, evidence path and remaining gaps.
- Main-session reasoning owns architecture, difficult debugging and security review. Delegate bounded implementation work per framework Section 9, with one owner per file; inspect transcripts for progress and do not repeat assigned work.
- Prefer a working connected slice and targeted regressions over test polishing. Fix failures that block the phase gate; record nonblocking issues without expanding scope. Do not collect tests from `Doc Corpus`.
- Continue to the next eligible task automatically during authorized development. Stop for authentication, unapproved costs or side effects, or a material deviation from the approved design. Never weaken a gate to claim completion.

## Timeline

Sequence is authoritative; working-session estimates are provisional, not delivery promises.

- **Session 1:** Phase 0 baseline and Phase 1 normal-profile delegation.
- **Session 2:** Phase 2 bank/run controls and Phase 3 ingestion support; parallelize only independent files after agreeing their interface.
- **Session 3:** Phase 4 connected rehearsal and live-test readiness decision.
- **Session 4:** Phase 5 live pilot, then Phase 6 rollout when its evidence is accepted.

Re-estimate after Phase 1 using observed throughput. Bank reconciliation, queue drain and account approvals can extend elapsed time. Do not reopen approved architectural decisions merely to fill a phase.

## Phase 0 - Establish the migration baseline

**Governing design:** Sections 2, 7 and 8. **Dependency:** approved framework (satisfied).

- [ ] Inventory active workers, run claims and pending ingestion; establish ownership before changes.
- [ ] Preserve existing configuration, user changes, source corpora and operation receipts in a rollback baseline.
- [ ] Classify existing modules as retain, adapt or retire; assign a single supervisor and helper entrypoint.
- [ ] Verify that the chosen normal Hermes execution mode matches the approved security tradeoff. Escalate only an additional requirement or material deviation.

**Outcome / gate:** migration scope and rollback paths recorded; no uncertain active work will be overwritten or silently retired.

## Phase 1 - Build the on-demand Hermes profile and delegation path

**Governing design:** Sections 1-4. **Dependency:** Phase 0.

- [ ] Inspect the supported Hermes profile-loading and invocation mechanisms; adapt the existing launcher rather than create another agent runtime.
- [ ] Assign profile instructions to their sole owners from Section 2: identity, protocol, workspace boundaries, settings and tool procedures. Remove duplicated policy text and verify actual loading.
- [ ] Configure Librarian's static model through Hermes; verify it does not inherit the caller's model or change per task. Do not apply implementation-worker routing to Librarian.
- [ ] Update the caller skill/request validation to trigger on documentation not already ingested into the caller's Hindsight bank and pass only the permitted facts.
- [ ] Verify invocation starts the agent, task termination stops it, and durable progress survives shutdown. Run an unfamiliar-software discovery smoke through this exact path with ingestion disabled.

**Outcome / gate:** ordinary Hermes delegation works without a custom provider/client/RPC stack; submitted request, loaded instructions, model identity and process cleanup are evidenced.

## Phase 2 - Enforce bank-first reuse and duplicate-run protection

**Governing design:** Sections 5A, 5E and 6. **Dependency:** Phase 1 invocation interface.

- [ ] Connect the caller-bound Hindsight inventory and local ledger; preserve explicit existing, pending, missing and unknown dispositions.
- [ ] Make verified content reusable and pending work attachable. Reconcile uncertain historical metadata before writes; do not require a content hash before read-only discovery.
- [ ] Acquire run ownership before spawning and renew/release it with owner fencing. Implement explicit stale-run recovery using recorded process identity.
- [ ] Exercise concurrent duplicate launches, verified reuse, pending ingestion and interruption. Confirm no duplicate worker or automatic repost.

**Outcome / gate:** bank-first and ownership checks run on every invocation, including resumed runs, without preventing legitimate discovery.

## Phase 3 - Connect complete acquisition and the ingestion helper

**Governing design:** Sections 5B-5F and 6. **Dependency:** fixed Phase 2 inventory/claim interface.

- [ ] Enable normal Librarian tools for evidence-backed identity, version, official-source and dependency discovery; no product-specific approval branches.
- [ ] Implement index accounting: attempt official `llms.txt`, reconcile other first-party enumerators and generate a local index when absent. Record each component's boundary and unresolved gaps.
- [ ] Adapt reusable acquisition/preparation utilities; preserve originals and classify documentation versus assets. Extraction failures remain unresolved.
- [ ] Connect one helper to validate hashes/provenance, claim content before POST, persist actual operation IDs and submit only to the bound bank.
- [ ] Connect bounded helper monitoring and ledger resume without a resident LLM. Separate acceptance, terminal ingestion and recall readiness in one report.

**Outcome / gate:** normal agent-acquired documentation traverses the helper; a simulated accepted-but-pending submission survives restart without another POST. Source coverage and ingestion evidence remain distinct.

## Phase 4 - Verify readiness for bounded live testing

**Governing design:** Section 10 live-test gate. **Dependency:** Phases 1-3 connected.

- [ ] Run the normal delegation entrypoint for unfamiliar software through discovery, indexing, acquisition and preparation, using a clearly labeled fixture ingestion endpoint.
- [ ] Verify scope resistance to caller task context, complete declared component accounting, and truthful handling of unknown bank state and extraction gaps.
- [ ] Interrupt after submission acceptance; resume through the supported entrypoint and verify no duplicate submission, correct claim ownership and process cleanup.
- [ ] Perform main-session review of credentials, destinations, permissions and reports. Resolve functional or safety blockers without expanding architecture.
- [ ] Publish the verified live-test command, explicit budgets/write boundary, stop/recovery instructions, evidence locations and rollback procedure.

**Outcome / gate:** **READY FOR BOUNDED LIVE TEST** only when the connected evidence satisfies framework Section 10. List simulated services explicitly. This is not production readiness or proof of complete coverage for arbitrary software.

## Phase 5 - Execute the bounded live pilot

**Governing design:** Section 10 production gate. **Dependency:** Phase 4; authorization covers the pilot's actual writes and costs.

- [ ] Select a manageable first-party documentation target with a complete declared scope and explicit limits; resolve bank reuse before submission.
- [ ] Run real discovery, acquisition and new-content ingestion through the same supported entrypoint; preserve receipts and source indexes.
- [ ] Verify actual terminal operation results, scoped recall and interrupted resume without duplicate submissions. If the queue exceeds the window, preserve pending status for a later bounded helper invocation.
- [ ] Record coverage limitations and cleanup; obtain acceptance of the live result before rollout.

**Outcome / gate:** real acquisition, ingestion and resume verified for the declared pilot. Acceptance alone or a partial run does not pass.

## Phase 6 - Roll out and retire redundant paths

**Governing design:** Sections 3, 8 and 9. **Dependency:** Phase 5 accepted.

- [ ] Enable the normal caller workflow and supported hooks; document which checks are enforced and which are advisory.
- [ ] Validate another unfamiliar software/dependency combination without adding product branches.
- [ ] Retire the bespoke provider/client/RPC/image path after replacement verification; retain rollback records and pending-work evidence.
- [ ] Remove only verified unused scratch/build artifacts. Update skills/runbooks so one supported path remains.
- [ ] Review token usage, rework, throughput and failure rates; record optional tuning separately from rollout blockers.

**Outcome / gate:** one documented operational workflow, no active redundant runtime, and all retained work accounted for.

## Working file map

Existing paths are candidates for reuse, not mandates to preserve obsolete interfaces:

- Supervisor/request: `tools/librarian-launch.py`, `broker/librarian_request.py`.
- Profile: `/home/jsondarula/.hermes/profiles/librarian/SOUL.md`, `OPERATING_PROTOCOL.md` and `config.yaml`; applicable workspace `AGENTS.md`.
- Caller skill: `/home/jsondarula/.hermes/skills/autonomous-ai-agents/documentation-librarian/SKILL.md`.
- Inventory/ownership: `broker/hindsight_inventory.py`, `broker/corpus_preflight.py`.
- Acquisition/ingestion: `broker/acquisition_controller.py`, `broker/controller_integrations.py`, `broker/transport.py`, `broker/document_payload_policy.py`.
- Verification: `broker/librarian_completion.py`, focused `broker/test_*.py` modules; adapt one acceptance runner to the normal profile.

Record final helper/runbook paths in Phase 0. Test commands must name the changed modules; the acceptance command must use the actual implemented CLI, not an invented interface.

## Phase record

For each phase append a concise record here:

`Phase | owner | pending/in_progress/blocked/complete | verified outcome | command/result | evidence path | remaining gaps | rollback`

Phase 6 (bank-first reuse reconciliation) | Liv | in_progress | Bank-first reuse gate resolves already-ingested content: live read-only status over `phase6-normal-production-reuse-v3-20260919` reports 28/28 `verified-present` at complete coverage (18977/18977), previously 28/28 `unknown`/reconciliation-required | `tools/librarian-ingest.py status <manifest> --inventory-max-pages 500 --inventory-detail-reads 0 --status-deadline-seconds 900` → coverage complete, 28/28 verified-present; `pytest broker/` → 478 passed, 1 pre-existing unrelated failure | `Doc Corpus/reports/runs/phase6-normal-production-reuse-v3-20260919/reconciliation-report.md`, `postfix-status-reconciled.json` | No live submission in this change; Phase 6 target-validation on unfamiliar software, redundant-path retirement and metrics review still open; pre-existing red test `test_trusted_launch_passes_injected_browser_to_isolated_client` untouched | `.hermes/plans/baselines/reconcile-20260930T083220/`

Phase 6 (bank reuse identity fixes): `broker/corpus_preflight.py` compares `product`/`version`/`source` only when the bank row exposes them (conflict still refuses), proves reuse from the exact prepared SHA-256 plus a terminal caller/bank-scoped durable claim, and returns `unknown` rather than `missing` for bank bytes with no corroborating claim so nothing is re-posted. `broker/hindsight_inventory.py` accepts the artifact digest recorded in the retained context (`artifact_sha256` etc., artifact-scoped keys only) because Hindsight's own `content_hash` covers the retained payload, not the prepared bytes.

A phase passes only when its exit gate is evidenced. Framework changes require user approval; implementation details may be adjusted within that boundary and recorded here. The roadmap remains authoritative as development proceeds.
