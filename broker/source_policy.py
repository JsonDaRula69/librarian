"""Controller-side canonical identity and dependency policy (no model authority).

Pass an instance to DiscoveryController(controller, policy). Call bootstrap()
from the trusted launcher, then acquire every approved source, and require
assert_complete() in addition to the normal completion gate. No agent proposal
can shrink the request or the dependency closure. repositories maps canonical
HTTPS origins to controller-provisioned LOCAL Git repositories. The launcher
must authenticate that provisioning; git remote configuration is not evidence.

Only safe GET metadata is fetched here; no clone, package execution, or POST.
IntegratedController enumerates every blob under pinned documentation roots and
writes local-index.json, even without llms.txt. Linux uses Documentation; OpenCode
uses packages/web/src/content/docs. Other npm packages require a matching pinned
package.json directories.doc descriptor (plus conventional package README files).
Unknown layouts block, never fall back to whole code repositories or task pages.
This is documentation-root closure, not external website/manual coverage.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote

from .acquisition_controller import BoundaryError
from .librarian_discovery import SourceApproval, validate_proposal
from .librarian_request import validate_request
from .safe_egress import fetch as safe_fetch


class Unresolved(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _github(value):
    if isinstance(value, dict):
        value = value.get('url')
    if not isinstance(value, str):
        raise Unresolved('missing-canonical-repository')
    match = re.fullmatch(r'(?:git\+)?https://github.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?', value)
    if not match or any(part in ('.', '..') for part in match.groups()):
        raise Unresolved('unsupported-canonical-repository')
    return 'https://github.com/' + '/'.join(match.groups())


class CanonicalSourcePolicy:
    """Trusted callable: (proposal, evidence_snapshot) -> SourceApproval | None.

    Unknown versions, ranges, local plugins, unproven pins and unavailable
    metadata are explicit unresolved coverage, never latest-version guesses.
    fetch uses the safe_egress.fetch signature and is injectable for offline QA.
    """
    policy_id = 'canonical-source-v2'

    def __init__(self, controller, request, *, repositories, fetch=None, configurations=None):
        self.controller = controller
        self.request = copy.deepcopy(validate_request(request))
        self.repositories = {url: Path(path).absolute() for url, path in repositories.items()}
        self.fetch = fetch or safe_fetch
        self.rows: dict[str, dict] = {p['id']: dict(product_id=p['id'], version=p['version'],
                                 status='unresolved', reason='unsupported-product')
                     for p in self.request['products']}
        self.approvals = {}
        self.edges = []
        self.metadata = {}
        self.controller._state['source_policy'] = {'policy_id': self.policy_id,
            'requested_products': copy.deepcopy(self.request['products'])}

        for parent, raw in (configurations or {}).items():
            if parent not in self.rows or parent not in ('opencode', 'opencode-ai'):
                raise BoundaryError('configuration parent must be requested OpenCode')
            config = json.loads(raw)
            plugins = config.get('plugin', [])
            if not isinstance(plugins, list) or len(plugins) > 100:
                raise BoundaryError('invalid plugin configuration')
            for plugin in plugins:
                if not isinstance(plugin, str):
                    raise BoundaryError('invalid plugin identity')
                name, separator, version = plugin.rpartition('@')
                if not separator or not name:
                    name, version = plugin, 'unknown'
                if not re.fullmatch(r'(?:@[a-z0-9._-]+/)?[a-z0-9._-]+', name):
                    raise BoundaryError('local or malformed plugin requires separate adapter')
                self._dependency(parent, name, version, {'sha256': hashlib.sha256(raw if isinstance(raw, bytes) else raw.encode()).hexdigest(),
                    'kind': 'configuration'}, 'configured-plugin')
        self._publish()

    def _linux(self, version):
        if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?(?:-rc\d+)?', version):
            raise Unresolved('exact-kernel-version-required')
        url = 'https://www.kernel.org/releases.json'
        data = self._json(url)
        if not any(r.get('version') == version for r in data.get('releases', [])):
            raise Unresolved('kernel-release-not-in-official-index')
        origin = 'https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git'
        repo = self.repositories.get(origin)
        if repo is None:
            raise Unresolved('canonical-repository-not-provisioned')
        pin = self.controller._git(repo, 'rev-parse', '--verify', 'refs/tags/v' + version + '^{commit}').decode().strip()
        return self._git_plan(origin, pin), {'url': url, 'sha256': self.metadata[url],
                                          'tag': 'v' + version, 'origin': origin}

    def _ubuntu(self, product, version):
        from .ubuntu_sources import UbuntuSourceAdapter

        # The adapter owns publication, archive and DSC validation.  Keep the
        # injected fetch callable so policy tests remain offline and bounded.
        return UbuntuSourceAdapter(fetch=self.fetch).resolve(product, version)

    def _json(self, url):
        def exact(target):
            if target != url:
                raise Unresolved('metadata-redirect-forbidden')
        response = self.fetch(url, max_bytes=4 * 1024 * 1024, validate=exact)
        if response.url != url or response.status != 200 or response.redirects:
            raise Unresolved('metadata-response-not-canonical')
        if len(response.data) > 4 * 1024 * 1024:
            raise Unresolved('metadata-size-limit')
        data = json.loads(response.data)
        if not isinstance(data, dict):
            raise Unresolved('invalid-metadata')
        self.metadata[url] = hashlib.sha256(response.data).hexdigest()
        return data

    def _git_plan(self, origin, pin):
        if not isinstance(pin, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', pin):
            raise Unresolved('invalid-commit-pin')
        repo = self.repositories.get(origin)
        if repo is None or not repo.is_dir():
            raise Unresolved('canonical-repository-not-provisioned')
        if self.controller._git(repo, 'cat-file', '-t', pin).strip() != b'commit':
            raise Unresolved('commit-not-present')
        plan = {'kind': 'git', 'repository': str(repo), 'commit': pin}
        if origin == 'https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git':
            self._documentation_roots(plan, ['Documentation'])
        return plan

    def _documentation_roots(self, plan, roots):
        # Resolve against the pinned tree, never the mutable checkout or a proposal.
        tree = self.controller._git(Path(plan['repository']), 'ls-tree', '-rz',
                                    '--full-tree', plan['commit'])
        entries = {}
        for record in tree.split(b'\0'):
            if record:
                meta, name = record.split(b'\t', 1)
                entries[name.decode('utf-8')] = meta.decode('ascii').split()
        for root in roots:
            if (not isinstance(root, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', root)
                    or any(part in ('.', '..') for part in root.split('/'))):
                raise Unresolved('invalid-documentation-root')
            selected = [entry for name, entry in entries.items()
                        if name == root or name.startswith(root + '/')]
            if not selected:
                raise Unresolved('documentation-root-not-present')
            if any(kind != 'blob' or mode not in ('100644', '100755') for mode, kind, oid in selected):
                raise Unresolved('documentation-root-requires-separate-adapter')
        plan['documentation_roots'] = sorted(set(roots))

    def _dependency(self, parent, name, version, evidence, relation):
        if not isinstance(name, str) or not isinstance(version, str):
            raise Unresolved('invalid-dependency-metadata')
        edge = {'dependency_of': parent, 'product_id': name, 'version': version,
                'evidence': evidence, 'relation': relation}
        if edge not in self.edges:
            self.edges.append(edge)
        row = self.rows.get(name)
        if row is None:
            if len(self.rows) >= 100:
                raise Unresolved('dependency-budget-exceeded')
            self.rows[name] = dict(product_id=name, version=version,
                                  status='unresolved', reason='dependency-not-resolved')
        elif row['version'] != version:
            self.rows[name]['reason'] = 'dependency-version-conflict'
            self.rows[name]['conflict'] = True

    def _npm(self, product, version):
        package = 'opencode-ai' if product in ('opencode', 'opencode-ai') else product
        if not re.fullmatch(r'(?:@[a-z0-9._-]+/)?[a-z0-9._-]+', package):
            raise Unresolved('unsupported-package-identity')
        if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?', version):
            raise Unresolved('exact-package-version-required')
        url = 'https://registry.npmjs.org/' + quote(package, safe='') + '/' + quote(version, safe='')
        data = self._json(url)
        if data.get('name') != package or data.get('version') != version:
            raise Unresolved('metadata-identity-mismatch')
        origin = _github(data.get('repository'))
        if product in ('opencode', 'opencode-ai') and origin != 'https://github.com/anomalyco/opencode':
            raise Unresolved('metadata-repository-mismatch')
        proof = {'url': url, 'sha256': self.metadata[url]}
        for field in ('dependencies', 'optionalDependencies', 'peerDependencies'):
            dependencies = data.get(field, {})
            if not isinstance(dependencies, dict):
                raise Unresolved('invalid-dependency-metadata')
            for name, requirement in dependencies.items():
                self._dependency(product, name, requirement, proof, field)
        plan = self._git_plan(origin, data.get('gitHead'))
        if product in ('opencode', 'opencode-ai'):
            roots = ['packages/web/src/content/docs']
            proof['documentation_boundary'] = {'policy': 'opencode-website-docs-v1',
                                                'commit': plan['commit']}
        else:
            roots, descriptor = self._package_documentation(plan, package, version, data)
            proof['documentation_boundary'] = descriptor
        self._documentation_roots(plan, roots)
        return plan, proof

    def _package_documentation(self, plan, package, version, metadata):
        """npm directories.doc declares a complete doc directory, not page hints.

        Only a matching package.json blob in the authenticated, pinned repository
        can declare this boundary. Registry readmes/homepages and arbitrary links
        are not executable scope. Missing/unknown conventions require an adapter.
        """
        repository = metadata.get('repository')
        directory = repository.get('directory', '') if isinstance(repository, dict) else ''
        if (not isinstance(directory, str) or (directory and (
                not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*', directory)
                or any(p in ('.', '..') for p in directory.split('/'))))):
            raise Unresolved('invalid-package-directory')
        prefix = directory + '/' if directory else ''
        path = prefix + 'package.json'
        repo = Path(plan['repository'])
        records = self.controller._git(repo, 'ls-tree', '-z', plan['commit'], '--', path).split(b'\0')
        records = [r for r in records if r]
        if len(records) != 1:
            raise Unresolved('documentation-descriptor-required')
        meta, name = records[0].split(b'\t', 1)
        mode, kind, oid = meta.decode('ascii').split()
        if name.decode() != path or mode not in ('100644', '100755') or kind != 'blob':
            raise Unresolved('invalid-documentation-descriptor')
        if int(self.controller._git(repo, 'cat-file', '-s', oid)) > 1024 * 1024:
            raise Unresolved('documentation-descriptor-size-limit')
        raw = self.controller._git(repo, 'cat-file', 'blob', oid)
        descriptor = json.loads(raw)
        if (not isinstance(descriptor, dict) or descriptor.get('name') != package
                or descriptor.get('version') != version):
            raise Unresolved('documentation-descriptor-identity-mismatch')
        directories = descriptor.get('directories')
        doc = directories.get('doc') if isinstance(directories, dict) else None
        if (not isinstance(doc, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', doc)
                or any(part in ('.', '..') for part in doc.split('/'))):
            raise Unresolved('documentation-boundary-unresolved')
        roots = [prefix + doc]
        root_entry = self.controller._git(repo, 'ls-tree', '-z', plan['commit'], '--', roots[0])
        if root_entry and root_entry.split(b'\t', 1)[0].split()[1] != b'tree':
            raise Unresolved('documentation-root-must-be-directory')
        # Include conventional package overviews, but never infer closure from one.
        tree = self.controller._git(repo, 'ls-tree', '-z', plan['commit'], '--',
                                    *[prefix + n for n in ('README', 'README.md', 'README.rst', 'README.txt')])
        for record in tree.split(b'\0'):
            if record:
                roots.append(record.split(b'\t', 1)[1].decode('utf-8'))
        return roots, {'policy': 'npm-directories-doc-v1', 'path': path, 'blob': oid,
                       'sha256': hashlib.sha256(raw).hexdigest(), 'commit': plan['commit']}

    def __call__(self, proposal, evidence):
        try:
            kind = 'component' if 'dependency_of' in proposal else 'source'
            validate_proposal(kind, proposal)
            product, version = proposal['product_id'], proposal['version']
            row = self.rows.get(product)
            if (row is None or row['version'] != version or row.get('conflict')
                    or evidence.get('run_id') != self.controller._state['run_id']):
                return None
            if kind == 'component':
                parents = [e for e in self.edges if e['product_id'] == product
                           and e['version'] == version and e['dependency_of'] == proposal['dependency_of']]
                parent = self.approvals.get(proposal['dependency_of'])
                if not parents or parent is None or proposal['evidence_source_ids'] != [parent.source_id]:
                    raise Unresolved('dependency-relationship-unverified')
                if evidence.get('sources', {}).get(parent.source_id, {}).get('status') != 'complete':
                    raise Unresolved('dependency-source-incomplete')
            if product in self.approvals:
                return copy.deepcopy(self.approvals[product])
            if product in ('linux', 'linux-kernel'):
                plan, proof = self._linux(version)
            elif product.startswith('ubuntu/'):
                plan, proof = self._ubuntu(product, version)
            elif product in ('opencode', 'opencode-ai') or any(e['product_id'] == product for e in self.edges):
                plan, proof = self._npm(product, version)
            else:
                raise Unresolved('unsupported-product')
            identifier = 'canonical-' + _digest([product, version, plan])[:32]
            approval = SourceApproval(identifier, plan, self.policy_id)
            self.approvals[product] = approval
            row.update(status='approved', reason='awaiting-acquisition', source_id=identifier,
                       plan=copy.deepcopy(plan), authority=proof)
            self._publish()
            return copy.deepcopy(approval)
        except Exception as exc:
            if isinstance(proposal, dict) and proposal.get('product_id') in self.rows:
                self.rows[proposal['product_id']]['reason'] = str(exc) if isinstance(exc, Unresolved) else 'resolution-failed'
            self._publish()
            return None

    def _publish(self):
        self.controller._state['source_policy'].update(products=copy.deepcopy(list(self.rows.values())),
            dependencies=copy.deepcopy(self.edges), metadata=copy.deepcopy(self.metadata))

    def coverage(self):
        state = self.controller.evidence().snapshot()
        rows = copy.deepcopy(list(self.rows.values()))
        for row in rows:
            requested = next((p for p in self.request['products'] if p['id'] == row['product_id']), {})
            if requested.get('dependency_evidence'):
                row.update(status='unresolved', reason='local-evidence-reference-adapter-required')
                continue
            source = state['sources'].get(row.get('source_id'), {})
            plan = row.get('plan', {})
            same_plan = state['plan'].get(row.get('source_id')) == plan
            if plan.get('kind') == 'ubuntu-source':
                identity = (source.get('archive_manifest_sha256') ==
                            plan.get('archive_manifest_sha256'))
            else:
                identity = source.get('commit') == plan.get('commit')
            row['status'] = 'covered' if (not row.get('conflict')
                and source.get('status') == 'complete' and source.get('occurrences')
                and identity and same_plan) else 'unresolved'

        dependencies = copy.deepcopy(self.edges)
        receipt_dependencies = []
        seen_dependencies = {(d.get('dependency_of'), d.get('product_id'),
                              d.get('version'), d.get('requirement')) for d in dependencies}
        for source_id, source in state['sources'].items():
            if state['plan'].get(source_id, {}).get('kind') != 'ubuntu-source':
                continue
            for dependency in source.get('dependencies', []):
                receipt_dependencies.append(dependency)
                key = (dependency.get('dependency_of'), dependency.get('product_id'),
                       dependency.get('version'), dependency.get('requirement'))
                if key not in seen_dependencies:
                    dependencies.append(copy.deepcopy(dependency))
                    seen_dependencies.add(key)
                product_id = dependency.get('product_id')
                if not isinstance(product_id, str):
                    continue
                if not any(existing.get('product_id') == product_id for existing in rows):
                    rows.append({'product_id': product_id,
                                 'version': dependency.get('version', 'unknown'),
                                 'status': 'unresolved',
                                 'reason': dependency.get('reason', 'dependency-unresolved'),
                                 'dependency_of': dependency.get('dependency_of'),
                                 'requirement': dependency.get('requirement'),
                                 'evidence': copy.deepcopy(dependency.get('evidence', {}))})
        return {'status': 'ready' if rows and all(r['status'] == 'covered' for r in rows)
                and all(d.get('status') == 'resolved' for d in receipt_dependencies) else 'blocked',
                'products': rows, 'dependencies': dependencies}

    def assert_complete(self):
        if self.coverage()['status'] != 'ready':
            raise BoundaryError('canonical product/dependency coverage unresolved')

    def bootstrap(self, discovery):
        """Seed every requested identity, regardless of agent proposal behavior."""
        results = []
        for product in list(self.rows.values()):
            proposal = {'product_id': product['product_id'],
                        'version': product['version'], 'candidate_id': 'canonical'}
            try:
                validate_proposal('source', proposal)
            except ValueError:
                product['reason'] = 'exact-package-version-required'
                continue
            results.append(discovery.propose('source', proposal))
        self._publish()
        return results
