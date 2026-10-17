"""Evidence-backed, product-agnostic source registration.

The model can suggest an identity and URL, but only a controller-owned
DiscoveryBrowser receipt can turn that suggestion into an acquisition plan.
Authority is an explicit chain: a registered candidate receipt must be linked
from evidence at an operator/package supplied trusted origin. HTTPS alone is
not authority.
"""
from __future__ import annotations

import copy
import re
import json
import subprocess
from urllib.parse import urljoin, urlsplit

from .librarian_request import validate_request

from .acquisition_controller import BoundaryError
from .librarian_discovery import SourceApproval


class EvidenceUnresolved(ValueError):
    """Raised internally when evidence cannot establish a source boundary."""


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise EvidenceUnresolved("https-origin-required")
    return f"https://{parsed.hostname.lower()}" + (f":{parsed.port}" if parsed.port else "")


def _same_origin(left: str, right: str) -> bool:
    return _origin(left) == _origin(right)


class EvidenceSourcePolicy:
    """Resolve proposals from browser receipts, without product-name rules.

    ``authority_seeds`` is operator/package metadata, not a product allowlist.
    It may be an iterable of trusted origin URLs or a mapping of origin to an
    opaque seed label. A candidate is accepted only when its receipt is linked
    by a fetched receipt whose origin is seeded. ``controller`` is optional for
    unit use, but when supplied its live evidence is used for completion.
    """

    policy_id = "evidence-source-v1"

    def __init__(self, controller, request, *, discovery_browser, authority_seeds,
                 authority_relationships=None, repositories=None):
        self.controller = controller
        self.browser = discovery_browser
        self.authority_seeds, seed_edges = self._seeds(authority_seeds)
        self.authority_relationships = self._relationships(
            seed_edges, authority_relationships)
        self.repositories = dict(repositories or {})
        self.request = copy.deepcopy(request)
        self._validate_request()
        self.candidates = {}
        self.approvals = {}
        self.rows = {p["id"]: {"product_id": p["id"], "version": p["version"],
                               "status": "unresolved", "reason": "candidate-unregistered"}
                     for p in request["products"]}
        self.dependencies = []
        if hasattr(controller, "_state"):
            controller._state["source_policy"] = self._state()

    @staticmethod
    def _seeds(seeds):
        metadata = seeds if isinstance(seeds, dict) else None
        values = metadata.keys() if metadata is not None else seeds
        if isinstance(values, (str, bytes)):
            raise BoundaryError("authority seeds must be origin metadata")
        try:
            values = list(values)
        except TypeError as exc:
            raise BoundaryError("authority seeds must be origin metadata") from exc
        result, edges = {}, {}
        for value in values:
            origin = _origin(value)
            result[origin] = True
            if metadata is not None:
                spec = metadata[value]
                if isinstance(spec, dict):
                    spec = spec.get("allowed_linked_origins",
                                    spec.get("linked_origins",
                                             spec.get("allowed_origins", ())))
                # Existing mapping values may be opaque operator labels.
                # Only URL-shaped values describe linked-origin edges.
                if isinstance(spec, (str, bytes)):
                    spec = (spec,) if str(spec).startswith("https://") else ()
                try:
                    edges[origin] = {_origin(target) for target in (spec or ())}
                except (TypeError, EvidenceUnresolved) as exc:
                    raise BoundaryError("authority relationships must be origin metadata") from exc
        if not result:
            raise BoundaryError("at least one authority seed is required")
        return result, edges

    @staticmethod
    def _relationships(seed_edges, relationships):
        result = {origin: set(targets) for origin, targets in seed_edges.items()}
        if relationships is None:
            return result
        if not isinstance(relationships, dict):
            raise BoundaryError("authority relationships must be origin metadata")
        for source, targets in relationships.items():
            try:
                source_origin = _origin(source)
                if isinstance(targets, (str, bytes)):
                    targets = (targets,)
                result.setdefault(source_origin, set()).update(
                    _origin(target) for target in targets)
            except (TypeError, EvidenceUnresolved) as exc:
                raise BoundaryError("authority relationships must be origin metadata") from exc
        return result

    def _validate_request(self):
        try:
            validate_request(self.request)
        except (TypeError, ValueError) as exc:
            raise BoundaryError("invalid evidence source request") from exc

    def _state(self):
        return {"policy_id": self.policy_id, "request": copy.deepcopy(self.request),
                "products": copy.deepcopy(list(self.rows.values())),
                "dependencies": copy.deepcopy(self.dependencies)}

    def _publish(self):
        if hasattr(self.controller, "_state"):
            self.controller._state["source_policy"] = self._state()

    def register_candidate(self, *, candidate_id, receipt_id, authority_receipt_id):
        """Bind an LLM candidate to immutable controller browser receipts.

        ``receipt_id`` is the fetched candidate page. ``authority_receipt_id``
        is the fetched seed/index receipt that must contain a link to it.
        """
        if not isinstance(candidate_id, str) or not candidate_id:
            raise BoundaryError("candidate_id required")
        candidate = self.browser._get(receipt_id)
        authority = self.browser._get(authority_receipt_id)
        if authority.status != 200 or not authority.links:
            raise EvidenceUnresolved("authority-receipt-incomplete")
        if _origin(authority.final_url) not in self.authority_seeds:
            raise EvidenceUnresolved("authority-origin-unseeded")
        linked = {link["url"] for link in authority.links}
        if candidate.final_url not in linked and candidate.requested_url not in linked:
            raise EvidenceUnresolved("candidate-not-linked-by-receipt")
        if candidate.status != 200 or candidate.redirects:
            raise EvidenceUnresolved("candidate-receipt-not-canonical")
        authority_origin = _origin(authority.final_url)
        candidate_origin = _origin(candidate.final_url)
        if (candidate_origin != authority_origin and
                candidate_origin not in self.authority_relationships.get(authority_origin, set())):
            raise EvidenceUnresolved("candidate-origin-untrusted")
        record = {"candidate_id": candidate_id, "receipt_id": receipt_id,
                  "authority_receipt_id": authority_receipt_id,
                  "url": candidate.final_url,
                  "authority_origin": authority_origin,
                  "authority": "seed->linked-receipt"}
        self.candidates[candidate_id] = record
        return copy.deepcopy(record)

    def __call__(self, proposal, evidence):
        try:
            product = proposal["product_id"]
            row = self.rows.get(product)
            if not row or row["version"] != proposal["version"]:
                return None
            candidate_id = proposal.get("candidate_id")
            candidate = self.candidates.get(candidate_id)
            if candidate is None:
                row["reason"] = "candidate-unregistered"
                self._publish()
                return None
            if product in self.approvals:
                return copy.deepcopy(self.approvals[product])
            plan = self._plan(candidate)
            source_id = candidate["receipt_id"]
            approval = SourceApproval(source_id, plan, self.policy_id)
            self.approvals[product] = approval
            row.update(status="approved", reason="awaiting-acquisition", source_id=source_id,
                       candidate_id=candidate_id, authority=copy.deepcopy(candidate),
                       plan=copy.deepcopy(plan))
            self._publish()
            return copy.deepcopy(approval)
        except (KeyError, EvidenceUnresolved, BoundaryError, ValueError):
            if isinstance(proposal, dict) and proposal.get("product_id") in self.rows:
                self.rows[proposal["product_id"]]["reason"] = "evidence-unresolved"
                self._publish()
            return None

    def _plan(self, candidate):
        url = candidate["url"]
        # A file such as llms.txt enumerates its containing directory.  Keep
        # the trailing slash so AcquisitionController accepts its children.
        boundary = urljoin(url, ".")
        if not _same_origin(url, boundary):
            raise EvidenceUnresolved("candidate-origin-boundary")
        return {"kind": "index", "url": url, "boundary": boundary}

    def git_plan(self, *, candidate_id, authority_receipt_id, repository_origin,
                 commit, documentation_roots):
        """Build a Git plan only for a controller-provisioned repository.

        The path is never accepted from a proposal.  ``repositories`` is
        controller configuration, while roots must be enumerated in the
        trusted browser receipt (the model cannot invent a closure).
        """
        candidate = self.candidates.get(candidate_id)
        if not candidate or candidate["authority_receipt_id"] != authority_receipt_id:
            raise EvidenceUnresolved("candidate-not-registered")
        path = self.repositories.get(repository_origin)
        if not isinstance(path, str) or not path.startswith("/"):
            raise EvidenceUnresolved("controller-repository-not-provisioned")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise EvidenceUnresolved("invalid-commit-pin")
        if not isinstance(documentation_roots, list) or not documentation_roots:
            raise EvidenceUnresolved("documentation-roots-required")
        authority = self.browser._get(authority_receipt_id)
        try:
            manifest = json.loads(authority.raw.decode("utf-8"))
            enumerated = manifest["enumerator"]["roots"]
            if manifest["enumerator"].get("complete") is not True or not isinstance(enumerated, list):
                raise KeyError("enumerator")
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise EvidenceUnresolved("documentation-roots-not-enumerated") from exc
        if any((not isinstance(root, str) or root.startswith("/") or ".." in root.split("/")
                or root not in enumerated) for root in documentation_roots):
            raise EvidenceUnresolved("documentation-roots-not-enumerated")
        try:
            present = subprocess.run(["git", "-C", path, "cat-file", "-t", commit],
                                     check=True, capture_output=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise EvidenceUnresolved("controller-repository-unavailable") from exc
        if present != b"commit":
            raise EvidenceUnresolved("commit-not-present")
        try:
            tree = subprocess.run(["git", "-C", path, "ls-tree", "-r", "--name-only", commit],
                                  check=True, capture_output=True, timeout=5).stdout.decode().splitlines()
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
            raise EvidenceUnresolved("repository-enumeration-unavailable") from exc
        for root in documentation_roots:
            prefix = root.rstrip("/") + "/"
            if not any(entry == root.rstrip("/") or entry.startswith(prefix) for entry in tree):
                raise EvidenceUnresolved("documentation-root-not-in-repository")
        return {"kind": "git", "repository": path, "commit": commit,
                "documentation_roots": sorted(set(documentation_roots))}

    def record_dependency(self, *, dependency_of, product_id, version, evidence_receipt_id, role):
        """Record an edge only from structured evidence at the approved parent origin."""
        parent = self.rows.get(dependency_of)
        target = self.rows.get(product_id)
        if not parent or not target or dependency_of == product_id:
            raise EvidenceUnresolved("dependency-not-in-fetched-evidence")
        if dependency_of not in self.approvals:
            raise EvidenceUnresolved("dependency-parent-not-approved")
        receipt = self.browser._get(evidence_receipt_id)
        if _origin(receipt.final_url) != _origin(parent["authority"]["url"]):
            raise EvidenceUnresolved("dependency-evidence-origin-unapproved")
        if not isinstance(role, str) or not role:
            raise EvidenceUnresolved("dependency-role-invalid")
        try:
            document = json.loads(receipt.raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceUnresolved("dependency-evidence-not-structured") from exc
        if not self._manifest_edge(document, dependency_of, product_id, version):
            raise EvidenceUnresolved("dependency-not-in-structured-evidence")
        edge = {"dependency_of": dependency_of, "product_id": product_id,
                "version": version, "role": role, "evidence_receipt_id": evidence_receipt_id,
                "evidence_sha256": receipt.sha256}
        if edge not in self.dependencies:
            self.dependencies.append(edge)
        self._publish()
        return copy.deepcopy(edge)

    @staticmethod
    def _manifest_edge(value, parent, product_id, version):
        if isinstance(value, dict):
            name = value.get("product_id", value.get("name", value.get("id")))
            actual_version = value.get("version", value.get("resolved", value.get("versionRange")))
            owner = value.get("dependency_of", value.get("parent"))
            if name == product_id and (actual_version is None or actual_version == version) and (owner is None or owner == parent):
                return True
            return any(EvidenceSourcePolicy._manifest_edge(v, parent, product_id, version) for v in value.values())
        if isinstance(value, list):
            return any(EvidenceSourcePolicy._manifest_edge(v, parent, product_id, version) for v in value)
        return False

    @staticmethod
    def _receipt_has_content(receipt):
        entries, occurrences = receipt.get("entries"), receipt.get("occurrences")
        return (receipt.get("status") == "complete" and isinstance(entries, list) and bool(entries)
                and isinstance(occurrences, list) and len(occurrences) == len(entries)
                and all(isinstance(item.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                        for item in occurrences))

    def _computed_coverage(self):
        state = self.controller.evidence().snapshot() if hasattr(self.controller, "evidence") else {}
        sources = state.get("sources", {})
        covered = {}
        for product_id, row in self.rows.items():
            source_id = row.get("source_id")
            covered[product_id] = bool(source_id in sources and hasattr(self.controller, "plan")
                                       and self.controller.plan.get(source_id) == row.get("plan")
                                       and self._receipt_has_content(sources[source_id]))
        return covered, sources

    def coverage(self):
        covered, sources = self._computed_coverage()
        rows = copy.deepcopy(list(self.rows.values()))
        for row in rows:
            if covered.get(row["product_id"]):
                row["status"] = "covered"
        unresolved = [r for r in rows if r["status"] != "covered"]
        enumerated = any(s.get("complete_enumerator") is True and self._receipt_has_content(s) for s in sources.values())
        deps = self.unresolved_dependencies(covered)
        status = "ready" if not unresolved and not deps and enumerated else ("approved_sources_only" if not unresolved and not deps else "blocked")
        return {"status": status, "products": rows, "dependencies": copy.deepcopy(self.dependencies),
                "unresolved_dependencies": deps}

    def unresolved_dependencies(self, covered=None):
        covered = self._computed_coverage()[0] if covered is None else covered
        return [d for d in self.dependencies if not self._dependency_covered(d, covered)]

    def _dependency_covered(self, dependency, covered=None):
        covered = self._computed_coverage()[0] if covered is None else covered
        return bool(covered.get(dependency["product_id"]))

    def assert_complete(self):
        if self.coverage()["status"] != "ready":
            raise BoundaryError("evidence-backed source coverage unresolved")
