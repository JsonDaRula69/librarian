# Safe Live Discovery Trial

`live_trial.py` is an opt-in, bounded live entrypoint for a safe discovery-only
run. It uses the pinned Docker image in `image.json`, the existing
`DiscoveryBrowser`, `EvidenceSourcePolicy`, read-only `HindsightInventoryReader`
for the fixed `Liv` bank, low-reasoning Luna through the host-only
`LiteLLMInferenceAdapter`, and `RunClaimStore`.

## Safety boundary

- The default stage is `discovery`; it performs no Hindsight `POST`, no ingestion,
  no production guard bypass, and no server restart.
- The fixed host credential source is `/home/jsondarula/.hermes/.env`; only the
  exact `HINDSIGHT_API_KEY` and `LITELLM_API_KEY` entries are read. Secrets never
  enter the isolated image.
- The bank inventory read is the first trusted operation. Inventory is bounded
  and an incomplete read returns `reconciliation_required`, not success.
- The authority seed is exposed in the trusted catalog as a URL. It is not put in
  the purpose-free request and is never treated as a product identity.
- Every result is retained at
  `Doc Corpus/reports/runs/<run-id>/live-trial.json`, including safe
  `error_type`/`stage` records and controller evidence. Model final text is not
  evidence.
- Acquisition is explicitly unsupported in this entrypoint. `--stage acquisition`
  is blocked even with an approved-plan path; there is no ingestion-write flag.
  Any future acquisition must keep HTTPS and file quotas in the trusted
  controller and require parent review.

## Usage

Validate the pinned image, fixed routes, and bounded read-only Liv inventory
without spending on the model:

```sh
python tools/librarian-runtime/live_trial.py \
  --request-file request.json \
  --authority-seed https://public.example/docs/index.md \
  --run-id live-preflight \
  --max-seconds 60 \
  --preflight-only
```

Run the bounded live discovery stage:

```sh
python tools/librarian-runtime/live_trial.py \
  --request-file request.json \
  --authority-seed https://public.example/docs/index.md \
  --run-id live-discovery \
  --max-seconds 120
```

`request.json` must be purpose-free schema-1 JSON accepted by
`broker.librarian_request`; it contains only product IDs, versions, and optional
structured dependency references. `--max-seconds` is limited to 180.

Exit `0` means the requested preflight passed or discovery executed cleanly;
exit `74` means blocked, partial, reconciliation-required, or unsupported. A
clean discovery execution is not an ingestion or corpus-completion claim.
