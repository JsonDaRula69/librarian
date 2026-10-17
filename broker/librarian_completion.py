"""Deterministic completion gate for Librarian corpus runs."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from broker.acquisition_controller import TrustedEvidence, safe_path, read_safe

REQUIRED_ARTIFACTS = {
    "result.json",
    "final-result.json",
    "enumerator-snapshots.json",
    "acquisition-manifest.json",
    "prepared-manifest.json",
    "coverage-matrix.json",
    "exclusions.json",
    "completeness-challenge.json",
    "summary.md",
}
TERMINAL_OCCURRENCE_STATES = {"collected", "unavailable", "excluded"}


def build_contract(
    *, run_id: str, caller_profile: str, workspace: Path, corpus_root: Path,
    hindsight_bank: str, ingestion_required: bool, requested_products=None,
) -> dict[str, Any]:
    return {
        "schema": "librarian-completion-contract-v2",
        "requested_products": copy.deepcopy(requested_products or []),
        "controller_evidence_required": True,
        "trust_input": "live controller.evidence() supplied by isolated trusted launcher; never a JSON path",
        "completion_semantics": "valid means completed; blocked and partial are not success",
        "deployment_requirements": [
            "controller memory/source plan inaccessible to agent",
            "separate OS authority and protected corpus mounts",
            "network egress policy including DNS rebinding protection",
            "trusted ingestion receipts bound to run, corpus, bank and prepared hashes",
        ],
        "run_id": run_id,
        "caller_profile": caller_profile,
        "workspace": str(workspace),
        "corpus_root": str(corpus_root),
        "hindsight_bank": hindsight_bank,
        "acquisition_scope": "complete_product_corpus",
        "ingestion_required": ingestion_required,
        "required_artifacts": sorted(REQUIRED_ARTIFACTS),
        "allowed_final_statuses": ["completed", "blocked", "partial"],
        "blocked_rule": (
            "Blocked requires at least one concrete source_failure with a source URL or repository ref, "
            "attempted fallback methods, and a source-level failure reason. Tool or command failure alone is invalid."
        ),
    }


def _load_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON {path.name}: {exc}")
        return None


def _records(data: Any, key: str, filename: str, errors: list[str]) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        errors.append(f"{filename} must contain a `{key}` array")
        return []
    rows = data[key]
    if not all(isinstance(row, dict) for row in rows):
        errors.append(f"{filename} `{key}` entries must be objects")
        return []
    return rows


def _validate_report(run_dir: Path, contract: dict[str, Any], *, trusted_evidence=None, candidate=False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    missing = sorted(name for name in REQUIRED_ARTIFACTS if not (run_dir / name).is_file())
    if missing:
        errors.append("missing required artifacts: " + ", ".join(missing))

    result = _load_json(run_dir / "result.json", errors) if (run_dir / "result.json").is_file() else None
    final = _load_json(run_dir / "final-result.json", errors) if (run_dir / "final-result.json").is_file() else None
    enum_data = _load_json(run_dir / "enumerator-snapshots.json", errors) if (run_dir / "enumerator-snapshots.json").is_file() else None
    acquisition = _load_json(run_dir / "acquisition-manifest.json", errors) if (run_dir / "acquisition-manifest.json").is_file() else None
    prepared = _load_json(run_dir / "prepared-manifest.json", errors) if (run_dir / "prepared-manifest.json").is_file() else None
    coverage = _load_json(run_dir / "coverage-matrix.json", errors) if (run_dir / "coverage-matrix.json").is_file() else None
    challenge = _load_json(run_dir / "completeness-challenge.json", errors) if (run_dir / "completeness-challenge.json").is_file() else None

    for name, data in (("result.json", result), ("final-result.json", final)):
        if not isinstance(data, dict):
            continue
        for field in ("run_id", "caller_profile", "workspace", "corpus_root", "hindsight_bank"):
            if data.get(field) != contract[field]:
                errors.append(f"{name} `{field}` does not match the run contract")
        if data.get("status") not in contract["allowed_final_statuses"]:
            errors.append(f"{name} has invalid terminal status")
    if isinstance(result, dict) and isinstance(final, dict) and result != final:
        errors.append("result.json and final-result.json must be identical")

    enumerators = _records(enum_data, "enumerators", "enumerator-snapshots.json", errors) if enum_data is not None else []
    occurrences = _records(acquisition, "occurrences", "acquisition-manifest.json", errors) if acquisition is not None else []
    documents = _records(prepared, "documents", "prepared-manifest.json", errors) if prepared is not None else []
    coverage_rows = _records(coverage, "rows", "coverage-matrix.json", errors) if coverage is not None else []
    challenges = _records(challenge, "challenges", "completeness-challenge.json", errors) if challenge is not None else []

    enum_ids = [row.get("id") for row in enumerators]
    if not enumerators or any(not isinstance(value, str) or not value for value in enum_ids):
        errors.append("every enumerator requires a non-empty `id`")
    if len(enum_ids) != len(set(enum_ids)):
        errors.append("enumerator IDs must be unique")
    for row in enumerators:
        if row.get("status") not in {"complete", "unavailable"}:
            errors.append(f"enumerator {row.get('id')!r} is not terminal")
        if not isinstance(row.get("occurrence_ids"), list):
            errors.append(f"enumerator {row.get('id')!r} requires `occurrence_ids`")
        if row.get("status") == "unavailable" and not row.get("source_failure_id"):
            errors.append(f"unavailable enumerator {row.get('id')!r} requires `source_failure_id`")

    occurrence_ids = [row.get("id") for row in occurrences]
    if not occurrences or any(not isinstance(value, str) or not value for value in occurrence_ids):
        errors.append("every occurrence requires a non-empty `id`")
    if len(occurrence_ids) != len(set(occurrence_ids)):
        errors.append("occurrence IDs must be unique")
    occurrence_set = set(occurrence_ids)
    enum_set = set(enum_ids)
    for row in occurrences:
        state = row.get("status")
        if state not in TERMINAL_OCCURRENCE_STATES:
            errors.append(f"occurrence {row.get('id')!r} is not terminal")
        refs = row.get("enumerator_ids")
        if not isinstance(refs, list) or not refs or not set(refs) <= enum_set:
            errors.append(f"occurrence {row.get('id')!r} has invalid enumerator references")
        if state == "collected":
            source_path = row.get("source_path")
            expected_hash = row.get("sha256")
            path = Path(contract["corpus_root"]) / source_path if isinstance(source_path, str) else None
            if path is None or not path.is_file():
                errors.append(f"collected occurrence {row.get('id')!r} source file is missing")
            elif not isinstance(expected_hash, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                errors.append(f"collected occurrence {row.get('id')!r} source hash mismatch")
        elif not row.get("reason"):
            errors.append(f"{state} occurrence {row.get('id')!r} requires a reason")

    enumerated_occurrences: set[str] = set()
    for row in enumerators:
        refs = row.get("occurrence_ids") or []
        unknown = set(refs) - occurrence_set
        if unknown:
            errors.append(f"enumerator {row.get('id')!r} references unknown occurrences")
        enumerated_occurrences.update(refs)
    if occurrence_set != enumerated_occurrences:
        errors.append("enumerator-to-occurrence parity failed")

    prepared_occurrences: set[str] = set()
    for row in documents:
        refs = row.get("occurrence_ids")
        path_value = row.get("path")
        expected_hash = row.get("sha256")
        if not isinstance(refs, list) or not refs or not set(refs) <= occurrence_set:
            errors.append("prepared document has invalid occurrence references")
            continue
        path = Path(contract["corpus_root"]) / path_value if isinstance(path_value, str) else None
        if path is None or not path.is_file() or path.suffix.lower() != ".md" or path.stat().st_size == 0:
            errors.append(f"prepared document path is invalid: {path_value!r}")
        elif not isinstance(expected_hash, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            errors.append(f"prepared document hash mismatch: {path_value!r}")
        prepared_occurrences.update(refs)
    # Binary/source assets are deliberately preserved as controller evidence,
    # not prepared documents. Only the live controller ledger can authorize
    # that exception; agent-authored exclusions are not evidence.
    asset_only_occurrences: set[str] = set()
    if type(trusted_evidence) is TrustedEvidence:
        state = trusted_evidence.snapshot()
        classifications = state.get("ingestion", {}).get("classifications", {})
        asset_only_occurrences = {
            occurrence_id for occurrence_id, classification in classifications.items()
            if isinstance(classification, dict)
            and classification.get("classification") == "asset-only"
        }
    collected_set = {row["id"] for row in occurrences if row.get("status") == "collected" and row.get("id")}
    if collected_set != prepared_occurrences | asset_only_occurrences:
        errors.append("not every collected occurrence maps to a prepared document")

    if not coverage_rows:
        errors.append("coverage matrix must contain rows")
    for row in coverage_rows:
        if row.get("status") not in {"covered", "unavailable", "excluded"}:
            errors.append(f"coverage row {row.get('surface')!r} is not terminal")
        refs = row.get("occurrence_ids")
        if not isinstance(refs, list) or not refs or not set(refs) <= occurrence_set:
            errors.append(f"coverage row {row.get('surface')!r} has invalid occurrence references")
    if not candidate and isinstance(coverage, dict) and coverage.get("complete") is not True:
        errors.append("coverage-matrix.json must set `complete` to true")

    if not challenges:
        errors.append("completeness challenge must contain attempts")
    for row in challenges:
        attempts = row.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            errors.append("each completeness challenge requires fallback attempts")
        if row.get("outcome") not in {"no_new_occurrences", "occurrences_added", "source_failure"}:
            errors.append("each completeness challenge requires a terminal outcome")
    if not candidate and isinstance(challenge, dict) and challenge.get("status") != "passed":
        errors.append("completeness challenge must pass")

    status = result.get("status") if isinstance(result, dict) else None
    source_failures = result.get("source_failures", []) if isinstance(result, dict) else []
    if status == "blocked":
        if not isinstance(source_failures, list) or not source_failures:
            errors.append("blocked status requires concrete `source_failures`")
        for failure in source_failures if isinstance(source_failures, list) else []:
            if not isinstance(failure, dict) or not (failure.get("source_url") or failure.get("repository_ref")):
                errors.append("each source failure requires a URL or repository ref")
            if not isinstance(failure.get("attempts"), list) or len(failure.get("attempts", [])) < 2:
                errors.append("each source failure requires at least two fallback attempts")
            if not failure.get("reason"):
                errors.append("each source failure requires a source-level reason")
    elif source_failures:
        warnings.append("completed run contains source failures")

    counts = result.get("counts") if isinstance(result, dict) else None
    expected_counts = {
        "enumerators": len(enumerators),
        "occurrences": len(occurrences),
        "collected": len(collected_set),
        "unavailable": sum(row.get("status") == "unavailable" for row in occurrences),
        "excluded": sum(row.get("status") == "excluded" for row in occurrences),
        "prepared": len(documents),
        "unique_hashes": len({row.get("sha256") for row in documents if row.get("sha256")}),
    }
    if not isinstance(counts, dict):
        errors.append("result.json requires exact `counts`")
    else:
        for key, value in expected_counts.items():
            if counts.get(key) != value:
                errors.append(f"result count `{key}` must equal {value}")

    if contract["ingestion_required"] and (candidate or status == "completed"):
        submission = result.get("submission") if isinstance(result, dict) else None
        if not isinstance(submission, dict) or submission.get("accepted") != expected_counts["unique_hashes"]:
            errors.append("completed ingestion run must accept every unique prepared hash")

    return {
        "schema": "librarian-completion-validation-v1",
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "computed_counts": expected_counts,
    }


def _requested_coverage_errors(contract, state):
    """Bind literal request identities to controller-owned policy receipts.

    This checks approved coverage, not exhaustive live source discovery. Neither
    agent report rows nor a preseeded acquisition plan proves product identity.
    """
    from broker.librarian_request import validate_request
    products = contract.get('requested_products')
    try:
        validate_request({'schema_version': 1, 'products': products})
    except ValueError:
        return ['requested products are missing or invalid in completion contract']
    discovery = state.get('discovery', {})
    approvals = []
    for proposal_id, record in discovery.get('proposals', {}).items():
        receipt = discovery.get('receipts', {}).get(record.get('receipt_id'), {})
        source_id = receipt.get('source_id')
        if (record.get('status') == 'approved' and receipt.get('policy_id')
                and receipt.get('run_id') == contract['run_id']
                and receipt.get('proposal_id') == proposal_id
                and receipt.get('receipt_id') == record.get('receipt_id')
                and record.get('source_ids') == [source_id]
                and source_id in state['plan']
                and receipt.get('plan') == state['plan'][source_id]
                and state['sources'].get(source_id, {}).get('status') == 'complete'
                and state['sources'][source_id].get('occurrences')):
            approvals.append((record, receipt))
    errors = []
    for product in products:
        matches = [(r, receipt) for r, receipt in approvals
                   if r.get('proposal', {}).get('product_id') == product['id']
                   and r['proposal'].get('version') == product['version']]
        if not matches:
            errors.append(f"requested product {product['id']}@{product['version']} lacks trusted identity coverage")
        for dependency in product.get('dependency_evidence', []):
            if not any(r.get('kind') == 'component'
                       and r['proposal'].get('dependency_of') == dependency['dependency_of']
                       and dependency in receipt.get('dependency_evidence', [])
                       and receipt.get('evidence_source_ids')
                       and receipt['evidence_source_ids'] == r['proposal'].get('evidence_source_ids')
                       and all(state['sources'].get(i, {}).get('status') == 'complete'
                               for i in receipt['evidence_source_ids'])
                       for r, receipt in matches):
                errors.append(f"requested product {product['id']} lacks trusted dependency evidence")
    return errors


def validate_run(run_dir: Path, contract: dict[str, Any], *, trusted_evidence=None, candidate=False) -> dict[str, Any]:
    """Fail closed unless the trusted launcher supplies its live controller handle.

    This does not authenticate same-UID callers or secure the ingestion broker.
    `valid` is reserved for completed runs; blocked/partial are never success.
    """
    errors = []
    state = None
    if type(trusted_evidence) is not TrustedEvidence:
        errors.append('trusted controller evidence is required')
    else:
        state = trusted_evidence.snapshot()
    try:
        root = Path(contract['corpus_root'])
        run_dir = Path(run_dir).absolute()
        relative_run = run_dir.relative_to(root.absolute())
        for name in REQUIRED_ARTIFACTS:
            safe_path(root, str(relative_run / name))
        # Preflight all file references before the legacy structural checks read them.
        for name, key, field in [('acquisition-manifest.json', 'occurrences', 'source_path'),
                                 ('prepared-manifest.json', 'documents', 'path')]:
            path = run_dir / name
            if path.is_file():
                data = json.loads(path.read_text())
                for row in data.get(key, []):
                    if field in row:
                        read_safe(root, row[field])
        verdict = _validate_report(run_dir, contract, trusted_evidence=trusted_evidence, candidate=candidate)
        errors.extend(verdict['errors'])
        result = json.loads((run_dir / 'result.json').read_text()) if (run_dir / 'result.json').is_file() else {}
        if state is not None:
            errors.extend(_requested_coverage_errors(contract, state))
            if state['run_id'] != contract['run_id'] or state['corpus_root'] != str(root.absolute()):
                errors.append('controller evidence belongs to another run or corpus')
            if not state['plan'] or set(state['sources']) != set(state['plan']):
                errors.append('controller source plan is not fully attempted')
            expected = []
            attempts = {}
            for source, receipt in state['sources'].items():
                if receipt['status'] != 'complete':
                    errors.append(f'controller source {source} is {receipt["status"]}')
                expected.extend(receipt['occurrences'])
                attempts.update({row['id']: row for row in receipt['attempts']})
                for path_key, hash_key in [('index_path', 'index_sha256'),
                                            ('upstream_index_path', 'upstream_index_sha256')]:
                    if path_key in receipt and hashlib.sha256(read_safe(root, receipt[path_key])).hexdigest() != receipt[hash_key]:
                        errors.append('controller index hash mismatch')
                if receipt['status'] == 'complete':
                    if state['plan'][source]['kind'] == 'git':
                        tree = {(r['path'], r['blob']) for r in receipt['entries']}
                        copied = {(r['path'], r['blob']) for r in receipt['occurrences']}
                    else:
                        tree = {r['source_url'] for r in receipt['entries']}
                        copied = {r['source_url'] for r in receipt['occurrences']}
                    if not tree or tree != copied:
                        errors.append('controller full path/blob parity failed')
            acquisition = json.loads((run_dir / 'acquisition-manifest.json').read_text())
            actual = acquisition['occurrences']
            if len(actual) != len(expected) or {r['id'] for r in actual} != {r['id'] for r in expected}:
                errors.append('controller-to-report full occurrence parity failed')
            lookup = {r['id']: r for r in actual}
            for row in expected:
                claimed = lookup.get(row['id'], {})
                if any(claimed.get(key) != value for key, value in row.items()):
                    errors.append(f'controller provenance mismatch: {row["id"]}')
                if hashlib.sha256(read_safe(root, row['source_path'])).hexdigest() != row['sha256']:
                    errors.append('controller source hash mismatch')
            enums = json.loads((run_dir / 'enumerator-snapshots.json').read_text())['enumerators']
            if {r['id'] for r in enums} != set(state['plan']):
                errors.append('controller enumerator plan parity failed')
            for row in enums:
                receipt = state['sources'].get(row['id'], {})
                if set(row.get('occurrence_ids', [])) != {r['id'] for r in receipt.get('occurrences', [])}:
                    errors.append('controller enumerator occurrence parity failed')
            challenge = json.loads((run_dir / 'completeness-challenge.json').read_text())
            for row in challenge['challenges']:
                for attempt in row.get('attempts', []):
                    if not isinstance(attempt, str) or attempt not in attempts:
                        errors.append('fabricated or unproven challenge attempt')
            for failure in result.get('source_failures', []):
                for attempt in failure.get('attempts', []):
                    if not isinstance(attempt, str) or attempts.get(attempt, {}).get('status') != 'failed':
                        errors.append('fabricated or unproven source failure attempt')
        if not candidate and result.get('status') != 'completed':
            errors.append('blocked/partial report is not successful completion')
        if result.get('source_failures'):
            errors.append('source failures prevent successful completion')
        if state is not None and 'discovery' in state:
            if any(p.get('status') != 'approved' for p in state['discovery']['proposals'].values()):
                errors.append('unresolved discovery proposals prevent completion')
        if contract.get('ingestion_required'):
            from broker.controller_integrations import validate_ingestion
            documents = json.loads((run_dir / 'prepared-manifest.json').read_text())['documents']
            errors.extend(validate_ingestion(trusted_evidence, contract, documents))
        verdict['status'] = 'completed' if not errors else ('blocked' if result.get('status') == 'blocked' else 'partial')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        errors.append(f'invalid or unsafe report: {exc}')
        verdict = {'warnings': [], 'computed_counts': {}, 'status': 'invalid'}
    verdict.update(schema='librarian-completion-validation-v2', valid=not errors, errors=errors)
    return verdict
