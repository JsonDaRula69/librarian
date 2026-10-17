import json
from types import SimpleNamespace

import pytest

from broker.discovery_browser import DiscoveryBrowser
from broker.evidence_source_policy import EvidenceSourcePolicy, EvidenceUnresolved


class Evidence:
    def snapshot(self):
        return {"sources": {}}


class Controller:
    def __init__(self):
        self._state = {}

    def evidence(self):
        return Evidence()


def browser_fixture():
    pages = {
        "https://docs.fictional.invalid/": b'<a href="/fable/1.0">Fable 1.0</a>',
        "https://docs.fictional.invalid/fable/1.0": b"Fable 1.0 documentation",
        "https://docs.fictional.invalid/other": b"Other page",
    }

    def fetch(url, **kwargs):
        return SimpleNamespace(url=url, status=200, redirects=(), data=pages[url])

    browser = DiscoveryBrowser(fetch=fetch)
    authority = browser.fetch("https://docs.fictional.invalid/")
    candidate = browser.fetch("https://docs.fictional.invalid/fable/1.0")
    return browser, authority["receipt_id"], candidate["receipt_id"]


def policy_fixture():
    browser, authority_id, candidate_id = browser_fixture()
    policy = EvidenceSourcePolicy(
        Controller(),
        {"schema_version": 1, "products": [{"id": "fable-tool", "version": "1.0"}]},
        discovery_browser=browser,
        authority_seeds={"https://docs.fictional.invalid": "operator-seed"},
    )
    return policy, authority_id, candidate_id


def test_unfamiliar_product_registers_from_linked_receipts():
    policy, authority_id, candidate_id = policy_fixture()
    record = policy.register_candidate(
        candidate_id="fable-candidate", receipt_id=candidate_id,
        authority_receipt_id=authority_id)
    assert record["authority"] == "seed->linked-receipt"
    approval = policy(
        {"product_id": "fable-tool", "version": "1.0", "candidate_id": "fable-candidate"},
        {"run_id": "unused"},
    )
    assert approval.source_id == candidate_id
    assert approval.plan == {
        "kind": "index",
        "url": "https://docs.fictional.invalid/fable/1.0",
        "boundary": "https://docs.fictional.invalid/fable/",
    }


def test_bad_origin_and_forged_receipt_cannot_register():
    policy, authority_id, candidate_id = policy_fixture()
    with pytest.raises(EvidenceUnresolved, match="authority-origin-unseeded"):
        policy.authority_seeds.clear()
        policy.register_candidate(candidate_id="x", receipt_id=candidate_id,
                                  authority_receipt_id=authority_id)

    policy, authority_id, candidate_id = policy_fixture()
    with pytest.raises(EvidenceUnresolved, match="candidate-not-linked"):
        # A valid receipt ID is not enough; the seed receipt must contain the link.
        other = policy.browser.fetch("https://docs.fictional.invalid/other")
        policy.register_candidate(candidate_id="x", receipt_id=other["receipt_id"],
                                  authority_receipt_id=authority_id)


def test_candidate_id_reuses_registered_receipt_and_unknown_stays_unresolved():
    policy, authority_id, candidate_id = policy_fixture()
    assert policy({"product_id": "fable-tool", "version": "1.0", "candidate_id": "x"}, {}) is None
    policy.register_candidate(candidate_id="x", receipt_id=candidate_id,
                              authority_receipt_id=authority_id)
    first = policy({"product_id": "fable-tool", "version": "1.0", "candidate_id": "x"}, {})
    second = policy({"product_id": "fable-tool", "version": "1.0", "candidate_id": "x"}, {})
    assert first.source_id == second.source_id == candidate_id


def test_cross_origin_requires_explicit_trusted_edge():
    pages = {
        "https://docs.fictional.invalid/": b'<a href="https://launchpad.invalid/fable">Fable</a><a href="https://random.invalid/fable">Random</a>',
        "https://launchpad.invalid/fable": b"Fable documentation",
        "https://random.invalid/fable": b"Random documentation",
    }

    def fetch(url, **kwargs):
        return SimpleNamespace(url=url, status=200, redirects=(), data=pages[url])

    browser = DiscoveryBrowser(fetch=fetch)
    authority = browser.fetch("https://docs.fictional.invalid/")
    candidate = browser.fetch("https://launchpad.invalid/fable")
    policy = EvidenceSourcePolicy(
        Controller(), {"schema_version": 1, "products": [{"id": "fable-tool", "version": "1.0"}]},
        discovery_browser=browser,
        authority_seeds={"https://docs.fictional.invalid": "operator-seed"},
        authority_relationships={"https://docs.fictional.invalid": {"https://launchpad.invalid"}},
    )
    record = policy.register_candidate(candidate_id="x", receipt_id=candidate["receipt_id"],
                                       authority_receipt_id=authority["receipt_id"])
    assert record["url"] == "https://launchpad.invalid/fable"

    random = browser.fetch("https://random.invalid/fable")
    with pytest.raises(EvidenceUnresolved, match="candidate-origin-untrusted"):
        policy.register_candidate(candidate_id="random", receipt_id=random["receipt_id"],
                                  authority_receipt_id=authority["receipt_id"])


def test_cross_origin_redirect_remains_unverified():
    pages = {"https://docs.fictional.invalid/": b'<a href="https://launchpad.invalid/fable">Fable</a><a href="https://random.invalid/fable">Random</a>',
             "https://launchpad.invalid/fable": b"Fable"}

    def fetch(url, **kwargs):
        return SimpleNamespace(url=url, status=200, redirects=("https://other.invalid/fable",), data=pages[url])

    browser = DiscoveryBrowser(fetch=fetch)
    authority = browser.fetch("https://docs.fictional.invalid/")
    candidate = browser.fetch("https://launchpad.invalid/fable")
    policy = EvidenceSourcePolicy(
        Controller(), {"schema_version": 1, "products": [{"id": "fable-tool", "version": "1.0"}]},
        discovery_browser=browser, authority_seeds={"https://docs.fictional.invalid": "seed"},
        authority_relationships={"https://docs.fictional.invalid": {"https://launchpad.invalid"}},
    )
    with pytest.raises(EvidenceUnresolved, match="candidate-receipt-not-canonical"):
        policy.register_candidate(candidate_id="x", receipt_id=candidate["receipt_id"],
                                  authority_receipt_id=authority["receipt_id"])


def test_dependency_role_requires_fetched_evidence():
    policy, authority_id, candidate_id = policy_fixture()
    policy.register_candidate(candidate_id="x", receipt_id=candidate_id,
                              authority_receipt_id=authority_id)
    with pytest.raises(EvidenceUnresolved, match="dependency-not-in-fetched"):
        policy.record_dependency(dependency_of="fable-tool", product_id="missing",
                                 version="1", evidence_receipt_id=candidate_id, role="runtime")


def test_git_plan_rejects_arbitrary_path_and_incomplete_enumerator():
    policy, authority_id, candidate_id = policy_fixture()
    policy.register_candidate(candidate_id="x", receipt_id=candidate_id,
                              authority_receipt_id=authority_id)
    with pytest.raises(EvidenceUnresolved, match="repository-not-provisioned"):
        policy.git_plan(candidate_id="x", authority_receipt_id=authority_id,
                        repository_origin="https://git.example.invalid/fable",
                        commit="a" * 40, documentation_roots=["docs"])

    with pytest.raises(EvidenceUnresolved, match="documentation-roots-not-enumerated"):
        policy.repositories["origin"] = "/tmp"
        policy.git_plan(candidate_id="x", authority_receipt_id=authority_id,
                        repository_origin="origin", commit="a" * 40,
                        documentation_roots=["docs"])
