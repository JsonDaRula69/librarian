# Librarian: Proportionate Design Framework

Status: proposed replacement architecture for review. This document is not authorization to migrate, retire code, or relax isolation. Companion: `2026-09-07_161153-librarian-development-roadmap.md`.

## 1. Purpose and success definition

Librarian is a reusable, reasoning-driven documentation acquisition and ingestion agent. It is invoked when an agent encounters software, a package, a tool or an integration whose complete authoritative first-party documentation has not already been ingested into the requesting agent's Hindsight bank, or when a user explicitly requests documentation acquisition and ingestion. Librarian first checks that bank and its associated ingestion ledger, reuses verified existing documentation, and independently discovers, acquires and ingests missing or updated documentation. Its scope includes the complete authoritative first-party documentation corpus for the software and its relevant operational dependencies, plugins and integrations.

Success is measured at two distinct milestones: readiness for a bounded live test, and readiness for production rollout. Section 10 defines the evidence required for each; neither milestone implies that every possible software product is supported perfectly.

## 2. Core architecture

Use normal Hermes execution rather than maintaining a second agent runtime:

1. **Caller skill:** recognizes the ingestion gap defined in Section 1 and delegates using the contract in Section 4.
2. **Normal Librarian profile:** a dedicated Hermes profile assembled from the following pieces, each with one responsibility and one authoritative home for its instructions:
   - **`SOUL.md` - identity and mission:** defines who Librarian is, its enduring purpose as a complete-documentation acquisition and ingestion agent, and its evidence-oriented judgment. It does not contain step-by-step procedures, tool commands, run-specific facts or copies of operational rules.
   - **`OPERATING_PROTOCOL.md` - operational authority:** owns the detailed rules and workflow described in this framework, including bank-first checks, independent discovery, complete-corpus scope, sourcing, preparation, ingestion, verification and recovery. Other pieces reference this protocol rather than reproducing it.
   - **`AGENTS.md` - workspace boundaries:** owns local workspace conventions and caller-isolation boundaries. It points to the operating protocol for Librarian behavior; it does not restate the mission or acquisition procedure. Applicable workspace instructions must be checked for conflicts before launch.
   - **`config.yaml` - runtime settings:** selects the model/provider, reasoning settings, enabled tools, permissions and supported hooks. It contains settings rather than behavioral prose. Credentials remain outside prompts and are supplied through the authorized host-side credential mechanism.
   - **Skills - reusable tool procedures:** contain specialized instructions for using research, extraction and ingestion tools. They explain how to perform a tool-specific operation, not when to narrow scope or what constitutes overall completion. The caller's delegation skill belongs to the requesting agent and is not copied into Librarian's profile.
   - **Hermes system-prompt assembly - loading, not another policy document:** the normal runtime combines its base instructions with the profile identity, applicable workspace instructions and the canonical protocol through a documented loading mechanism. There is no separately maintained copy of Librarian's mission or protocol inside a second custom system prompt. Verify that the protocol is actually loaded before work starts; a path reference alone is insufficient. Skills are loaded when needed and are not also pasted wholesale into the system prompt.
   - **Per-run request - facts only:** contains only the invocation facts defined in Section 4; it is not a persistent profile instruction layer.

   **Non-duplication rule:** update each instruction only in its authoritative piece. Other pieces use references; they do not paraphrase the same requirements into competing policy text. Identity states the mission, the protocol defines the operating rules, workspace instructions define local boundaries, configuration selects capabilities, and skills describe tool procedures. If these pieces conflict, correct the source documents before execution rather than inventing a new precedence rule. This is the proposed profile organization, not a claim that the current installed profile already implements it.
3. **One run supervisor:** binds the caller, corpus and bank; owns run claims, process lifecycle and cleanup.
4. **One ingestion helper:** validates artifacts, preserves provenance, performs durable hash deduplication, submits to the bound bank and resumes operation monitoring.
5. **One verification report:** separates coverage judgment from objective acquisition and ingestion evidence.

**Static model assignment:** Librarian has its own model assigned through the normal Hermes profile configuration. That assignment is static across calls until explicitly changed by the operator; Librarian does not inherit the calling agent's model, select a model per task, or switch models autonomously. Hermes handles provider connectivity and inference. "Do not implement another model provider" means do not build a separate provider adapter or model-routing layer for Librarian; it does not mean Librarian lacks a configured model.

**On-demand lifecycle:** Librarian is not an always-on agent. The supervisor starts an instance of its configured Hermes profile when called, and shuts that instance down when its task is complete. If a run is blocked or interrupted, it saves its progress and exits rather than remaining idle indefinitely. A later call resumes from durable records without duplicating completed work. The profile definition persists between calls; the agent process does not. The caller owns the corpus and Hindsight bank, and durable run/ingestion records survive process shutdown.

Do not implement a separate JSON decision loop, discovery-only client, RPC vocabulary or per-product policy engine. Reuse documented Hermes mechanisms where they actually exist; investigate gaps instead of assuming feature availability.

## 3. Enforcement boundaries

Section 2 defines the profile components and their instruction ownership. This section defines how behavior is checked, without introducing another policy layer.

Use supported hooks for reminders and checks at known action boundaries. Verify that each hook is loaded and determine its failure behavior before relying on it. Hooks supplement the supervisor and helper; their security limits are described in Section 7.

The supervisor enforces run binding and ownership independently of the model's final response. The helper owns submission identity and operation receipts. Detailed execution order belongs to Section 5, and outcome meanings belong to Section 6.

## 4. Request contract

Required facts: product/tool identities and known versions. Trusted launcher configuration supplies caller profile, absolute workspace/corpus root, bank, run ID and budgets. Optional inputs: canonical project/package references and structured environment/plugin/dependency manifests. These are evidence inputs, not exhaustive scope declarations.

No `purpose`, task narrative or free-form instructions field. Only the user can authorize a subset; record that authorization separately from caller-generated content. Unknown versions may be resolved through discovery rather than guessed.

## 5. Normal workflow

### A. Bank-first and run reuse

- Check the caller's Hindsight inventory plus local source/ingestion ledger before downloading or submitting.
- Recall can help find relevant records but cannot prove absence or completeness.
- Compare product, version, canonical source, document identity and hashes where available.
- Reuse verified existing coverage. Attach to pending ingestion. Resolve ambiguous prior submissions without blind reposting.
- An incomplete inventory is unknown, not empty. It permits read-only discovery, but not an assumption that everything should be re-ingested.
- Claim the caller/bank/product/version scope before launching another worker. Renew ownership, detect conflicts and require explicit recovery for uncertain stale claims.
- Do not require an unknown source hash before allowing discovery; content deduplication occurs once actual content is available.

### B. Independent discovery

The LLM establishes software identity, official sources, installed/proposed versions and adjacent components using project sites, package metadata, repositories and environment evidence. Dependency relevance determines which components enter scope; it must not select a handful of pages within an included component.

Capture each dependency as included, verified-existing, outside the operational boundary with rationale, or unresolved. Avoid unbounded recursion into every incidental transitive development package. Escalate genuine boundary ambiguity rather than silently shrinking scope.

### C. First-party indexing

Attempt official `llms.txt` and related published indexes. Reconcile them with sitemaps, site navigation and repository documentation trees; an index may be incomplete. If absent, generate a local provenance-backed index. Record source enumerators, versions, commits and acquisition dates.

Cover documentation formats and surfaces: authored guides, reference/API schemas, ABI text, installation, configuration, migration, compatibility, release history, security/advisories and recovery guidance. Relevant official issue/release records are evidence, not a mandate to ingest all discussions on a hosting service.

Follow cross-domain official links using provenance and identity reasoning. Do not require a bespoke allowlist edge for every legitimate vendor domain. Network safety checks remain separate from judgments of official authority.

### D. Acquire and prepare

Acquire all enumerated documentation for each scoped component. Preserve originals and source receipts. Downloading a whole source package is not itself proof of complete documentation.

Retain archives and binaries as evidence/assets without turning base64 into semantic documentation. Extract PDF/Office text with supported tools when needed; failed extraction stays unresolved. Preserve authored schemas, extensionless text and examples within documentation boundaries. Keep source classification and exclusions explicit.

### E. Deduplicate, ingest and resume

Maintain many-to-one source aliases separately from content bytes. Reuse unchanged content even when a path moves. Before POST, durably record a hash claim. Persist actual returned operation IDs immediately. If POST disposition is unknown after interruption, reconcile before retry.

Persist canonical URLs/repository pins, versions, source hashes, prepared hashes, caller/bank and operation status. Resume only unfinished work. A bounded helper invocation may monitor accepted operations without keeping the LLM agent resident; if monitoring must continue later, persist the pending state and invoke the helper again through the supervisor. Apply the lifecycle in Section 2 and the status meanings in Section 6.

### F. Verify and report

The supervisor/helper verifies files, hashes, inventory parity, classifications, bank binding, operation states and cleanup. The LLM provides a reasoned completeness challenge: what alternative indexes, versions and dependency surfaces were checked and what may remain missing.

Never conflate an internally consistent manifest with proof of every possible official source. Report objective verified coverage and residual semantic uncertainty separately.

## 6. Status model

- `discovering` / `acquiring` / `preparing`: active work.
- `ingestion_pending`: accepted work awaiting terminal status.
- `reconciliation_required`: ambiguous bank, submission or prior-run state.
- `blocked`: external boundary prevents progress, with evidence and next action.
- `partial`: some coverage or verification criteria remain unmet.
- `complete`: declared component/version boundaries verified and requested ingestion/recall criteria satisfied.

A successful agent exit or valid report schema never implies task completion. No new work, already present, and attached-to-existing-run are useful outcomes, not failures or reasons to submit duplicates.

## 7. Security and proportional enforcement

Hard requirements: separate caller corpora and banks; no credentials in prompts/logs; bound submission destination; no unauthorized sends/deployments; safe network/path handling; durable deduplication and auditable operations.

**Explicit tradeoff:** a normal local Hermes agent running as the same OS user is not cryptographically or OS-isolated from every writable file. Prompts, read-only tool settings and hooks reduce accidental drift but are not a hard defense against arbitrary same-user shell access. Do not describe them as equivalent to the custom whole-process sandbox.

Retain host-controlled credentials and ingestion where practical. Select existing documented sandbox/permission features if needed, without rebuilding an agent runtime. If adversarial tamper resistance remains mandatory, resolve that boundary before migration: it may require a separate OS identity or supported whole-agent sandbox. User approval of this tradeoff is a Phase 0 gate.

## 8. Existing work: reuse and retirement

Reuse after focused review: hash/operation ledger, claim logic, provenance handling, safe fetch helpers, document classification, relevant adversarial tests and historical evaluation evidence.

Adapt: `tools/librarian-launch.py`, caller skill, profile identity/protocol and ingestion helper entrypoint.

Stage for retirement only after replacement succeeds: bespoke `librarian_agent_client`, relay, inference bridge, image build and hardcoded source-policy dispatch. Preserve them and rollback evidence until migration is accepted; do not delete original corpora or active operation receipts.

## 9. Development and cost discipline

This section governs agents used to develop and maintain the implementation, not Librarian model selection. The main development session handles architecture, difficult debugging and security review. Implementation workers use OpenAI-only models: Luna/low for precise tasks, or Sol low/medium when complexity warrants. Do not modify running workers. Assign one owner per file and one worker per task; inspect transcripts and outputs for actual progress rather than relying on liveness. Librarian itself follows the static assignment in Section 2.

Prioritize a connected functional slice, then a small safe live trial. Add targeted regressions for real failure modes; avoid expanding generic infrastructure or repeatedly running broad suites. Do not collect corpus-contained test scripts as project tests. Record model usage when available, first-pass success and rework; recommend routing changes from evidence.

## 10. Acceptance boundaries

Required before live testing: a connected rehearsal demonstrates compliance with Sections 2-7 through the normal delegation entrypoint. Evidence must include the submitted request, loaded profile components, resolved model configuration, source inventory, bank/claim decisions, acquisition and submission receipts, interrupted-resume results and process cleanup. Verify startup on invocation and shutdown at a terminal run outcome. Publish a reproducible bounded command, known limitations and rollback procedure. A fixture may establish wiring, but must be labeled as such.

Required before production rollout: repeat the connected workflow against genuine first-party sources and the authorized Hindsight bank. Verify terminal ingestion status and scoped recall, then exercise reuse and interruption recovery without duplicate submissions. Evaluate an unfamiliar software target and its discovered dependencies without adding product-specific branches. A bounded pilot proves only its declared scope, not complete coverage of a wider ecosystem.

## References and interpretation

- Hermes persistent goals: https://hermes-agent.nousresearch.com/docs/user-guide/features/goals
- Hermes documentation index: https://hermes-agent.nousresearch.com/docs/llms.txt
- OmO Librarian implementation inspected as a simplicity reference: https://github.com/code-yeongyu/oh-my-openagent/blob/dev/src/agents/librarian.ts

OmO's research agent is inspiration for normal agent/tool use, not evidence that it implements complete-corpus ingestion. This framework specifies the additional workflow we require.
