"""Controller-owned discovery. Agent tokens are proposals, never authority.

The injected policy is trusted code, not a model: it must independently resolve
canonical sources and verify dependency evidence. No proposed token is opened as
a URL/path. Plans and policy receipts remain on the controller side. Deploy this
object outside the agent's OS authority, just like AcquisitionController.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import re
import secrets

from .acquisition_controller import BoundaryError, safe_path
from .librarian_request import _TOKEN, _token
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SourceApproval:
    source_id: str
    plan: dict
    policy_id: str


def _id(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None


def validate_proposal(kind, proposal):
    fields = {'product_id', 'version', 'candidate_id'}
    if kind == 'component':
        fields = {'product_id', 'version', 'dependency_of', 'evidence_source_ids'}
    if kind not in ('source', 'component') or type(proposal) is not dict or set(proposal) != fields:
        raise BoundaryError('invalid discovery proposal')
    for field in fields - {'evidence_source_ids'}:
        value = proposal[field]
        if field in ('product_id', 'dependency_of'):
            _token(value, _TOKEN, field)
            continue
        pattern = r'[A-Za-z0-9*][A-Za-z0-9._+*-]{0,99}' if field == 'version' else r'[A-Za-z0-9_-]{1,128}'
        if (field == 'version' and isinstance(proposal.get('product_id'), str)
                and proposal['product_id'].startswith('ubuntu/')):
            pattern = r'[0-9][A-Za-z0-9.+:~\-]{0,99}'
        if type(value) is not str or re.fullmatch(pattern, value) is None:
            raise BoundaryError('invalid discovery token')
    if kind == 'component':
        ids = proposal['evidence_source_ids']
        if (type(ids) is not list or not 1 <= len(ids) <= 32 or any(not _id(i) for i in ids)
                or len(set(ids)) != len(ids) or proposal['dependency_of'] == proposal['product_id']):
            raise BoundaryError('invalid dependency evidence')


class DiscoveryController:
    """Explicit source registry adapter; policy returns SourceApproval or None.

    None or a policy exception leaves an unresolved proposal. Retrying the same
    proposal reuses its ID; only trusted policy may resolve it. Component policy
    receives the live controller evidence snapshot and must verify the asserted
    dependency relationship, not just existence of an evidence source.
    """
    def __init__(self, controller, policy, *, max_proposals=64):
        if not callable(policy) or type(max_proposals) is not int or not 1 <= max_proposals <= 128:
            raise BoundaryError('invalid discovery configuration')
        if 'discovery' in controller._state:
            raise BoundaryError('discovery already configured')
        self.controller, self.policy = controller, policy
        self.max_proposals = max_proposals
        self.state = {'proposals': {}, 'receipts': {}, 'components': {}}
        controller._state['discovery'] = self.state

    def catalog(self):
        return {'sources': [{'source_id': i} for i in sorted(self.controller.plan)],
                'components': copy.deepcopy(list(self.state['components'].values())),
                'proposals': [self._public(p) for p in self.state['proposals'].values()]}

    def _public(self, record):
        return {k: copy.deepcopy(record[k]) for k in
                ('proposal_id', 'status', 'source_ids', 'receipt_id') if k in record}

    def propose(self, kind, proposal):
        validate_proposal(kind, proposal)
        record = next((p for p in self.state['proposals'].values()
                       if p['kind'] == kind and p['proposal'] == proposal), None)
        if record is None:
            if len(self.state['proposals']) >= self.max_proposals:
                raise BoundaryError('discovery budget exhausted')
            identifier = secrets.token_hex(16)
            record = {'proposal_id': identifier, 'kind': kind,
                      'proposal': copy.deepcopy(proposal), 'status': 'unresolved'}
            self.state['proposals'][identifier] = record
        if record['status'] == 'approved':
            return self._public(record)
        evidence = self.controller.evidence().snapshot()
        if kind == 'component' and any(evidence['sources'].get(i, {}).get('status') != 'complete'
                                       for i in proposal['evidence_source_ids']):
            return self._public(record)
        try:
            approval = self.policy(copy.deepcopy(proposal), evidence)
            if type(approval) is not SourceApproval or not _id(approval.source_id) or not _id(approval.policy_id):
                return self._public(record)
            plan = copy.deepcopy(approval.plan)
            if not self._valid_plan(plan):
                return self._public(record)
            # The trusted policy alone supplies executable acquisition parameters.
            existing = self.controller.plan.get(approval.source_id)
            if existing is not None and existing != plan:
                return self._public(record)
            receipt_id = secrets.token_hex(32)
            receipt = {'receipt_id': receipt_id, 'run_id': evidence['run_id'],
                       'proposal_id': record['proposal_id'], 'policy_id': approval.policy_id,
                       'source_id': approval.source_id, 'plan': copy.deepcopy(plan),
                       'evidence_source_ids': copy.deepcopy(proposal.get('evidence_source_ids', []))}
            self.controller.plan[approval.source_id] = plan
            self.controller._state['plan'][approval.source_id] = copy.deepcopy(plan)
            self.state['receipts'][receipt_id] = receipt
            if kind == 'component':
                self.state['components'][proposal['product_id']] = {
                    **copy.deepcopy(proposal), 'source_ids': [approval.source_id]}
            record.update(status='approved', source_ids=[approval.source_id], receipt_id=receipt_id)
        except Exception:
            # Provider errors can contain secrets; keep the proposal unresolved.
            pass
        return self._public(record)

    def _valid_plan(self, plan):
        if type(plan) is not dict:
            return False
        if plan.get('kind') == 'ubuntu-source':
            from .acquisition_controller import validate_ubuntu_plan
            try:
                validate_ubuntu_plan(plan)
            except (ValueError, TypeError, KeyError):
                return False
            return True
        if plan.get('kind') == 'index':
            if set(plan) != {'kind', 'url', 'boundary'}:
                return False
            for key in ('url', 'boundary'):
                url = urlsplit(plan[key])
                if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
                    return False
            return True
        if plan.get('kind') == 'git':
            if (set(plan) not in ({'kind', 'repository', 'commit'},
                                 {'kind', 'repository', 'commit', 'documentation_roots'})
                    or type(plan['commit']) is not str
                    or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', plan['commit']) is None
                    or type(plan['repository']) is not str or not plan['repository']):
                return False
            if 'documentation_roots' in plan:
                roots = plan['documentation_roots']
                if type(roots) is not list or not roots or len(set(roots)) != len(roots):
                    return False
                for root in roots:
                    safe_path(self.controller.root, root)
            return True
        return False

    def completion(self):
        unresolved = [p['proposal_id'] for p in self.state['proposals'].values()
                      if p['status'] != 'approved']
        dependencies = [copy.deepcopy(d) for source in self.controller._state['sources'].values()
                        for d in source.get('dependencies', []) if d.get('status') != 'resolved']
        missing = [source_id for source_id, plan in self.controller.plan.items()
                   if plan.get('kind') == 'ubuntu-source' and 'dependencies' not in
                   self.controller._state['sources'].get(source_id, {})]
        return {'status': 'blocked' if unresolved or dependencies or missing else 'ready',
                'unresolved_proposal_ids': unresolved, 'unresolved_dependencies': dependencies,
                'missing_dependency_source_ids': missing}

    def assert_complete(self):
        if self.completion()['status'] != 'ready':
            raise BoundaryError('unresolved discovery proposals prevent completion')
