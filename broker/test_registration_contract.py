"""Regression coverage for policy-to-relay candidate registration."""
from types import MethodType, SimpleNamespace

import pytest

from broker.acquisition_controller import AcquisitionController
from broker.discovery_browser import DiscoveryBrowser
from broker.evidence_source_policy import EvidenceSourcePolicy
from broker.librarian_discovery import DiscoveryController
from broker.librarian_relay import ControllerAdapter, RelayError


class _PolicyController(AcquisitionController):
    pass


def _fixture(tmp_path):
    pages = {
        "https://docs.example.test/": b'<a href="/product/1.0">Product 1.0</a>',
        "https://docs.example.test/product/1.0": b"Product 1.0 documentation",
    }

    def fetch(url, **_kwargs):
        return SimpleNamespace(url=url, status=200, redirects=(), data=pages[url])

    browser = DiscoveryBrowser(fetch=fetch)
    authority = browser.fetch("https://docs.example.test/")
    candidate = browser.fetch("https://docs.example.test/product/1.0")
    controller = _PolicyController("registration-contract", tmp_path / "corpus", {})
    policy = EvidenceSourcePolicy(
        controller,
        {"schema_version": 1, "products": [{"id": "product", "version": "1.0"}]},
        discovery_browser=browser,
        authority_seeds={"https://docs.example.test": "fixture-seed"},
    )
    discovery = DiscoveryController(controller, policy)
    adapter = ControllerAdapter(controller, discovery=discovery, browser=browser)
    return policy, adapter, authority["receipt_id"], candidate["receipt_id"]


def _add_policy_extra(policy, extra):
    original = policy.register_candidate

    def register(self, **kwargs):
        result = original(**kwargs)
        result.update(extra)
        return result

    policy.register_candidate = MethodType(register, policy)


def test_real_evidence_policy_registration_projects_only_relay_fields(tmp_path):
    policy, adapter, authority_id, candidate_id = _fixture(tmp_path)
    _add_policy_extra(policy, {
        "url": "https://docs.example.test/product/1.0",
    })

    result = adapter.discovery_register_candidate(
        "product-candidate", candidate_id, authority_id)

    assert result == {
        "candidate_id": "product-candidate",
        "receipt_id": candidate_id,
        "authority_receipt_id": authority_id,
        "authority_origin": "https://docs.example.test",
        "authority": "seed->linked-receipt",
    }
    assert "url" not in result
    assert "plan" not in result


def test_real_evidence_policy_unknown_registration_extra_is_rejected(tmp_path):
    policy, adapter, authority_id, candidate_id = _fixture(tmp_path)
    _add_policy_extra(policy, {"unexpected": "controller-private"})

    with pytest.raises(RelayError):
        adapter.discovery_register_candidate(
            "product-candidate", candidate_id, authority_id)
