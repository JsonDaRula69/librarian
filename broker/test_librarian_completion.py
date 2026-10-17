import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from broker.librarian_completion import build_contract, validate_run


class LibrarianCompletionTests(unittest.TestCase):
    def contract(self, root: Path, run_id: str = "run"):
        corpus = root / "Doc Corpus"
        corpus.mkdir()
        run = corpus / "reports" / "runs" / run_id
        run.mkdir(parents=True)
        contract = build_contract(
            run_id=run_id,
            caller_profile="default",
            workspace=root,
            corpus_root=corpus,
            hindsight_bank="Liv",
            ingestion_required=False,
        )
        (run / "completion-contract.json").write_text(json.dumps(contract))
        return corpus, run, contract

    def write_valid_run(self, root: Path, corpus: Path, run: Path, contract: dict):
        source = corpus / "sources" / "first-party" / "doc.txt"
        prepared = corpus / "prepared" / "doc.md"
        source.parent.mkdir(parents=True)
        prepared.parent.mkdir(parents=True)
        source.write_text("source")
        prepared.write_text("# source\n")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        prepared_hash = hashlib.sha256(prepared.read_bytes()).hexdigest()
        enum = {"enumerators": [{"id": "e1", "status": "complete", "occurrence_ids": ["o1"]}]}
        acquisition = {"occurrences": [{
            "id": "o1", "status": "collected", "enumerator_ids": ["e1"],
            "source_path": str(source.relative_to(corpus)), "sha256": source_hash,
        }]}
        prepared_data = {"documents": [{
            "path": str(prepared.relative_to(corpus)), "sha256": prepared_hash,
            "occurrence_ids": ["o1"],
        }]}
        coverage = {"complete": True, "rows": [{
            "surface": "reference", "status": "covered", "occurrence_ids": ["o1"],
        }]}
        challenge = {"status": "passed", "challenges": [{
            "query": "alias search", "attempts": ["index", "web"], "outcome": "no_new_occurrences",
        }]}
        result = {
            "run_id": contract["run_id"], "caller_profile": "default",
            "workspace": str(root), "corpus_root": str(corpus), "hindsight_bank": "Liv",
            "status": "completed", "source_failures": [],
            "counts": {"enumerators": 1, "occurrences": 1, "collected": 1,
                       "unavailable": 0, "excluded": 0, "prepared": 1, "unique_hashes": 1},
        }
        payloads = {
            "enumerator-snapshots.json": enum,
            "acquisition-manifest.json": acquisition,
            "prepared-manifest.json": prepared_data,
            "coverage-matrix.json": coverage,
            "completeness-challenge.json": challenge,
            "exclusions.json": {"exclusions": []},
            "result.json": result,
            "final-result.json": result,
        }
        for name, value in payloads.items():
            (run / name).write_text(json.dumps(value))
        (run / "summary.md").write_text("complete")

    def test_agent_authored_complete_run_fails_without_trust(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus, run, contract = self.contract(root)
            self.write_valid_run(root, corpus, run, contract)
            verdict = validate_run(run, contract)
        self.assertFalse(verdict["valid"])
        self.assertIn("trusted controller evidence is required", verdict["errors"])

    def test_gap_report_without_manifests_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, run, contract = self.contract(root)
            result = {
                "run_id": "run", "caller_profile": "default", "workspace": str(root),
                "corpus_root": str(root / "Doc Corpus"), "hindsight_bank": "Liv",
                "status": "blocked", "counts": {}, "source_failures": [],
            }
            (run / "result.json").write_text(json.dumps(result))
            (run / "final-result.json").write_text(json.dumps(result))
            verdict = validate_run(run, contract)
        self.assertFalse(verdict["valid"])
        self.assertTrue(any("missing required artifacts" in error for error in verdict["errors"]))
        self.assertTrue(any("concrete `source_failures`" in error for error in verdict["errors"]))

    def test_controller_backed_run_and_adversarial_mutations(self):
        from broker.acquisition_controller import AcquisitionController
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus, run, contract = self.contract(root)
            repo = root / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            for i in range(8):
                (repo / f'{i}.rst').write_text(f'doc {i}')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=t@example.org', 'commit', '-qm', 'fixture'], check=True)
            pin = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()
            controller = AcquisitionController('run', corpus, {'kernel': {'kind': 'git', 'repository': str(repo), 'commit': pin}})
            from broker.librarian_discovery import DiscoveryController, SourceApproval
            discovery = DiscoveryController(controller, lambda proposal, evidence:
                SourceApproval('kernel', controller.plan['kernel'], 'fixture-policy'))
            discovery.propose('source', dict(product_id='linux', version='6.8', candidate_id='fixture'))
            contract['requested_products'] = [{'id': 'linux', 'version': '6.8'}]
            receipt = controller.acquire('kernel')
            self.write_valid_run(root, corpus, run, contract)
            occurrences = receipt['occurrences']
            ids = [r['id'] for r in occurrences]
            payload = json.loads((run / 'prepared-manifest.json').read_text())
            payload['documents'][0]['occurrence_ids'] = ids
            (run / 'prepared-manifest.json').write_text(json.dumps(payload))
            (run / 'acquisition-manifest.json').write_text(json.dumps({'occurrences': occurrences}))
            (run / 'enumerator-snapshots.json').write_text(json.dumps({'enumerators': [{'id': 'kernel', 'status': 'complete', 'occurrence_ids': ids}]}))
            (run / 'coverage-matrix.json').write_text(json.dumps({'complete': True, 'rows': [{'surface': 'tree', 'status': 'covered', 'occurrence_ids': ids}]}))
            (run / 'completeness-challenge.json').write_text(json.dumps({'status': 'passed', 'challenges': [{'attempts': ['kernel:git-tree'], 'outcome': 'no_new_occurrences'}]}))
            result = json.loads((run / 'result.json').read_text())
            result['counts'].update(occurrences=8, collected=8)
            for name in ['result.json', 'final-result.json']:
                (run / name).write_text(json.dumps(result))
            verdict = validate_run(run, contract, trusted_evidence=controller.evidence())
            self.assertTrue(verdict['valid'], verdict['errors'])
            original = {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}
            def reset():
                for name, data in original.items():
                    (run / name).write_bytes(data)
            for mutation in ['subset', 'provenance', 'attempt', 'outside', 'symlink', 'hash', 'malformed']:
                with self.subTest(mutation=mutation):
                    reset()
                    rows = json.loads(json.dumps(occurrences))
                    if mutation == 'subset':
                        rows = rows[:6]
                        subset_ids = [r['id'] for r in rows]
                        for name, key, field in [('enumerator-snapshots.json', 'enumerators', 'occurrence_ids'),
                                                 ('prepared-manifest.json', 'documents', 'occurrence_ids'),
                                                 ('coverage-matrix.json', 'rows', 'occurrence_ids')]:
                            data = json.loads((run / name).read_text())
                            data[key][0][field] = subset_ids
                            (run / name).write_text(json.dumps(data))
                        subset_result = json.loads(json.dumps(result))
                        subset_result['counts'].update(occurrences=6, collected=6)
                        for name in ['result.json', 'final-result.json']:
                            (run / name).write_text(json.dumps(subset_result))
                    elif mutation == 'provenance':
                        del rows[0]['repository_ref']
                    elif mutation == 'outside':
                        rows[0]['source_path'] = str(root / 'repo' / '0.rst')
                    elif mutation == 'symlink':
                        (corpus / 'escape').symlink_to(repo / '0.rst')
                        rows[0]['source_path'] = 'escape'
                    elif mutation == 'hash':
                        rows[0]['sha256'] = '0' * 64
                    elif mutation == 'malformed':
                        rows[0]['id'] = []
                    elif mutation == 'attempt':
                        (run / 'completeness-challenge.json').write_text(json.dumps({'status': 'passed', 'challenges': [{'attempts': ['invented'], 'outcome': 'no_new_occurrences'}]}))
                    (run / 'acquisition-manifest.json').write_text(json.dumps({'occurrences': rows}))
                    verdict = validate_run(run, contract, trusted_evidence=controller.evidence())
                    self.assertFalse(verdict['valid'], mutation)

    def test_dict_cannot_supply_trusted_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus, run, contract = self.contract(root)
            self.write_valid_run(root, corpus, run, contract)
            verdict = validate_run(run, contract, trusted_evidence={'sources': {}})
            self.assertFalse(verdict['valid'])

    def test_tool_failure_is_not_valid_source_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus, run, contract = self.contract(root)
            self.write_valid_run(root, corpus, run, contract)
            result = json.loads((run / "result.json").read_text())
            result["status"] = "blocked"
            result["source_failures"] = [{"reason": "terminal command blocked", "attempts": ["terminal"]}]
            (run / "result.json").write_text(json.dumps(result))
            (run / "final-result.json").write_text(json.dumps(result))
            verdict = validate_run(run, contract)
        self.assertFalse(verdict["valid"])
        self.assertTrue(any("URL or repository ref" in error for error in verdict["errors"]))
        self.assertTrue(any("at least two fallback attempts" in error for error in verdict["errors"]))

    def test_enumerator_occurrence_parity_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus, run, contract = self.contract(root)
            self.write_valid_run(root, corpus, run, contract)
            data = json.loads((run / "enumerator-snapshots.json").read_text())
            data["enumerators"][0]["occurrence_ids"] = []
            (run / "enumerator-snapshots.json").write_text(json.dumps(data))
            verdict = validate_run(run, contract)
        self.assertFalse(verdict["valid"])
        self.assertIn("enumerator-to-occurrence parity failed", verdict["errors"])


if __name__ == "__main__":
    unittest.main()
