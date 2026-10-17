# Phase 4 Connected Rehearsal

Status: completed-resumed (not fresh acquisition; normal acquire-only timed out/interrupted after producing a partial prepared corpus).

## Reproducible command

```text
/home/jsondarula/.hermes/hermes-agent/venv/bin/python tools/librarian-connected-rehearsal.py
```

The driver invokes the existing normal launcher with the facts-only request, `--acquire-only`, 120-second budget, and 10-turn Hermes budget, reusing the existing ripgrep 14.1.0 archive. It then invokes the existing supervisor `--dev-manifest` path for `status`, one allowed TEST ONLY `submit`, and `resume`.

Evidence: `Doc Corpus/reports/runs/connected-rehearsal-ripgrep-phase4/connected-rehearsal.json`.

## Verified facts

- Request: `Doc Corpus/rehearsal/ripgrep-normal-rehearsal-phase4-20260908T002500Z/request.json`; schema v1 with only ripgrep/14.1.0 facts.
- Archive reused: `Doc Corpus/reports/runs/acquire-only-real-20260908T002500Z/sources/ripgrep/14.1.0/ripgrep-14.1.0.tar.gz`.
- Manifest: `backup-notes/dev-readme-acceptance-manifest.json`; first-party README bytes hash verified as `236b6ba13edc0ae9ddc34bc034336ccdb7f22d6145f19d5cc4860637a2a2d767`.
- Dev helper: status, submit, and resume all exited 0; submit prepared exactly one artifact; resume reported `submitted: 0`, preserving no duplicate POST.
- Hindsight dev endpoint `127.0.0.1:18888/health`: HTTP 200, database connected before and after.
- Normal receipt: profile `librarian`, caller `default`, bank `Liv`, max turns 10, run budget 120, ingestion/post disabled; it exited 130 after 122.557s and is therefore labeled resumed, not fresh.
- Static model expected by the librarian profile: `chatgpt/gpt-5.6-luna`. No terminal recall claim is made.

## Gaps

The normal run did not finish within the bounded budget and its acquisition result is partial; this is not Phase 5 production/live-pilot acceptance. The connected evidence uses the existing verified manifest and existing pending operation receipt, with no provider configuration, worker enablement, or production write.
