"""Opt-in trusted-controller integrations; not a launcher or agent-facing API.

Plans, adapter, controller memory and corpus must be outside agent authority.
Documentation roots are operator-approved canonical paths, never agent exclusions.
This module does not establish OS isolation or DNS-rebinding-safe egress.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import fcntl
from functools import wraps
import hashlib
import re
import ipaddress
import os
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from urllib.parse import urlsplit
from pathlib import Path

from .acquisition_controller import AcquisitionController, BoundaryError, safe_path, sha
from .tag_scheme import derive_documentation_tags, errors, validate

# Documentation submissions ride the bank's document-ingestion strategy instead of
# the bank default (``retain_default_strategy``, normally the concise live-session
# profile), so extracted docs use the verbose document profile.  A bank that does
# not declare the name logs a warning and keeps its resolved config, so naming it
# can never silently corrupt another bank's behaviour.
DOC_RETAIN_STRATEGY = 'Doc Ingestion'


def _repository_slug(repository_ref: object) -> str | None:
    """'org/name@sha12' from the controller's '<origin-url>@<sha>:<path>' form."""
    if not isinstance(repository_ref, str) or not repository_ref.strip():
        return None
    match = re.match(r'^(?P<origin>[^\s@]+)@(?P<sha>[0-9a-fA-F]{7,40})(?::|$)', repository_ref.strip())
    if not match:
        return None
    origin, sha = match.group('origin').rstrip('/'), match.group('sha')
    if origin.startswith('http'):
        origin = origin.split('://', 1)[1]
        for host in ('github.com', 'gitlab.com', 'codeberg.org', 'bitbucket.org'):
            if origin.startswith(host + '/'):
                origin = origin[len(host) + 1:]
                break
    parts = [part for part in origin.split('/') if part]
    if len(parts) < 2:
        return None
    return f'{parts[-2]}/{parts[-1]}@{sha}'


def documentation_tags(context: dict, *, strict: bool = True) -> list[str]:
    """Compliant tag set for one submitted document (TAG-SCHEME.md v2).

    Product/version come from controller state, publisher from the canonical URL,
    repo/commit from the pinned provenance when there is one.  The set is
    validated here and the submission fails closed rather than writing a tag set
    the scheme rejects.  ``strict=False`` is for read-only reporting paths that
    must show a defective set instead of aborting.
    """
    provenance = context.get('provenance') or []
    first = provenance[0] if provenance and isinstance(provenance[0], dict) else {}
    tags = derive_documentation_tags(
        product=context.get('product') or 'unknown',
        version=context.get('version') or 'unknown',
        canonical_url=first.get('source_url'),
        path=first.get('path'),
        repository_ref=_repository_slug(first.get('repository_ref')))
    findings = errors(validate(tags, profile='corpus', author='librarian'))
    if findings and strict:
        raise BoundaryError(f'non-compliant documentation tags: {findings[0].reason}')
    return tags


def artifact_tag_sets(ingestion, artifacts) -> dict:
    """Derived tag set per prepared artifact, from the context the retain carries."""
    tag_sets = {}
    for artifact in artifacts:
        try:
            context = ingestion._retention_context(artifact)
        except Exception:
            context = {}
        tag_sets[artifact['id']] = documentation_tags(context, strict=False)
    return tag_sets


class GitRemote:
    """Shallow bare fetch with deadline and sampled disk budget, no checkout.

    Disk sampling can overshoot between checks; use filesystem/process quotas for
    hard limits (not a wire-byte cap). HTTPS uses a non-expiring curl DNS pin
    after validating every DNS answer. OS egress remains defense in depth.
    """
    protocols = 'https'

    def __init__(self, *, timeout=120, max_repository_bytes=256 * 1024 * 1024):
        import math
        if (not math.isfinite(timeout) or timeout <= 0
                or type(max_repository_bytes) is not int or max_repository_bytes <= 0):
            raise BoundaryError('positive fetch limits required')
        self.timeout = timeout
        self.max_repository_bytes = max_repository_bytes

    def _validated_url(self, url):
        if not isinstance(url, str) or not re.fullmatch(r'https://[A-Za-z0-9.-]+/[A-Za-z0-9_./~-]+', url):
            raise BoundaryError('canonical HTTPS Git URL required')
        parsed = urlsplit(url)
        if any(p in ('', '.', '..') for p in parsed.path[1:].split('/')):
            raise BoundaryError('noncanonical Git URL path')
        from . import safe_egress
        try:
            _, address = safe_egress._resolve(parsed.hostname, 443, time.monotonic() + self.timeout)
        except (ValueError, OSError, TimeoutError) as exc:
            raise BoundaryError('public Git DNS resolution failed') from exc
        # No '+' prefix: curl must never expire this entry and re-resolve.
        address = f'[{address}]' if ':' in address else address
        return url, f'{parsed.hostname}:443:{address}'

    @contextmanager
    def acquire(self, url, commit):
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
            raise BoundaryError('full commit hash required')
        deadline = time.monotonic() + self.timeout
        validated = self._validated_url(url)
        # Existing trusted local-fixture subclasses explicitly opt out of HTTPS.
        if self.protocols == 'https':
            if not isinstance(validated, tuple) or len(validated) != 2:
                raise BoundaryError('pinned Git HTTPS transport required')
            url, resolve = validated
        else:
            url, resolve = validated, None
        with tempfile.TemporaryDirectory(prefix='controller-git-') as directory:
            repo = Path(directory) / 'repo'
            env = dict(PATH='/usr/bin:/bin', HOME=directory, LC_ALL='C',
                       GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                       GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1',
                       GIT_ALLOW_PROTOCOL=self.protocols)
            from .safe_egress import _remaining
            if resolve is not None:
                # Older Git silently ignores unknown -c keys: fail before networking.
                supported = subprocess.run(['/usr/bin/git', 'help', '--config'],
                    env=env, cwd=directory, check=True, capture_output=True,
                    timeout=_remaining(deadline))
                if b'http.curloptResolve' not in supported.stdout.splitlines():
                    raise BoundaryError('Git http.curloptResolve support required')
            base = ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'credential.helper=',
                    '-c', 'http.sslVerify=true', '-c', 'http.followRedirects=false', '-c', 'http.proxy=',
                    '-c', 'fetch.fsckObjects=true', '-c', 'maintenance.auto=false',
                    '-c', 'gc.auto=0', '-c', 'submodule.recurse=false']
            if resolve is not None:
                base += ['-c', 'http.curloptResolve=' + resolve]
            subprocess.run(base + ['init', '--bare', '--template=',
                '--object-format=' + ('sha1' if len(commit) == 40 else 'sha256'), str(repo)],
                env=env, cwd=directory, check=True, capture_output=True, timeout=_remaining(deadline))
            with tempfile.TemporaryFile() as output:
                _remaining(deadline)
                process = subprocess.Popen(base + ['-C', str(repo), 'fetch', '--depth=1',
                    '--no-tags', '--no-recurse-submodules', url, commit], env=env, cwd=directory,
                    stdout=output, stderr=output, start_new_session=True)
                try:
                    while True:
                        size = sum(p.stat().st_size for p in repo.rglob('*') if p.is_file())
                        if size + os.fstat(output.fileno()).st_size > self.max_repository_bytes:
                            raise BoundaryError('remote repository byte budget exceeded')
                        status = process.poll()
                        if status is not None:
                            if status:
                                raise BoundaryError('remote Git fetch failed')
                            break
                        if time.monotonic() >= deadline:
                            raise BoundaryError('remote Git fetch deadline exceeded')
                        time.sleep(0.02)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            result = subprocess.run(base + ['-C', str(repo), 'rev-parse', '--verify',
                'FETCH_HEAD^{commit}'], env=env, cwd=directory, check=True,
                capture_output=True, timeout=_remaining(deadline))
            if result.stdout.decode().strip() != commit:
                raise BoundaryError('remote commit mismatch')
            yield repo


class IntegratedController(AcquisitionController):
    """Git enumeration of every blob within explicit canonical documentation roots."""

    def __init__(self, *args, remote=None, max_total_bytes=256 * 1024 * 1024, **kwargs):
        super().__init__(*args, **kwargs)
        self.remote = remote or GitRemote()
        self.max_total_bytes = max_total_bytes

    def _tree(self, source, plan, receipt):
        repository = plan.get('repository', '')
        if isinstance(repository, str) and '://' in repository:
            attempt = dict(id=f'{source}:git-fetch', method='git-fetch',
                           repository_ref=f'{repository}@{plan.get("commit")}', status='failed')
            receipt['attempts'].append(attempt)
            with self.remote.acquire(repository, plan.get('commit')) as repo:
                local = dict(plan, repository=str(repo))
                self._local_tree(source, local, receipt, repository)
                attempt['status'] = 'succeeded'
        else:
            self._local_tree(source, plan, receipt, repository)

    def _local_tree(self, source, plan, receipt, origin):
        if set(plan) != {'kind', 'repository', 'commit', 'documentation_roots'}:
            raise BoundaryError('Git plan requires explicit documentation_roots')
        pin = plan['commit']
        if not isinstance(pin, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', pin):
            raise BoundaryError('full commit hash required')
        roots = plan['documentation_roots']
        if not isinstance(roots, list) or not roots or len(set(roots)) != len(roots):
            raise BoundaryError('nonempty unique documentation roots required')
        for root in roots:
            safe_path(self.root, root)
        repo = Path(plan['repository'])
        if not repo.is_absolute() or not repo.is_dir():
            raise BoundaryError('controller-managed local repository required')
        if self._git(repo, 'cat-file', '-t', pin).strip() != b'commit':
            raise BoundaryError('pin must identify a commit')
        tree = self._git(repo, 'ls-tree', '-rz', '--full-tree', pin)
        entries = []
        matched = set()
        for record in tree.split(b'\0'):
            if not record:
                continue
            meta, name = record.split(b'\t', 1)
            name = name.decode('utf-8')
            selected = {root for root in roots if name == root or name.startswith(root + '/')}
            if not selected:
                continue
            matched.update(selected)
            safe_path(self.root, name)
            mode, kind, oid = meta.decode('ascii').split()
            entries.append(dict(path=name, mode=mode, kind=kind, blob=oid))
        if matched != set(roots) or not entries or len(entries) > self.max_files:
            raise BoundaryError('missing documentation root or enumeration limit exceeded')
        receipt.update(entries=entries, commit=pin, documentation_roots=copy.deepcopy(roots),
                       tree_sha256=sha(tree))
        receipt['attempts'].append(dict(id=f'{source}:git-tree', method='git-ls-tree',
            repository_ref=f'{origin}@{pin}', status='succeeded', sha256=sha(tree)))
        total_bytes = 0
        for entry in entries:
            if entry['kind'] != 'blob' or entry['mode'] not in ('100644', '100755'):
                raise BoundaryError('symlink or submodule requires separate approved acquisition')
            size = int(self._git(repo, 'cat-file', '-s', entry['blob']))
            total_bytes += size
            if size > self.max_bytes or total_bytes > self.max_total_bytes:
                raise BoundaryError('blob size or total byte limit exceeded')
            data = self._git(repo, 'cat-file', 'blob', entry['blob'])
            algorithm = hashlib.sha1 if len(entry['blob']) == 40 else hashlib.sha256
            if algorithm(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != entry['blob']:
                raise BoundaryError('Git blob parity failed')
            location = self._write(f'sources/controller/{source}/files/{entry["path"]}', data)
            receipt['occurrences'].append(dict(id=f'{source}:{entry["path"]}', status='collected',
                enumerator_ids=[source], source_path=location, sha256=sha(data),
                path=entry['path'], blob=entry['blob'],
                repository_ref=f'{origin}@{pin}:{entry["path"]}'))
        self._index(source, receipt)


def _durable(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._ledger_lock():
            return method(self, *args, **kwargs)
    return locked


class ControllerIngestion:
    """Trusted preparation/retain adapter. Public submission accepts artifact IDs only.

    UTF-8 text is preserved verbatim. Binary assets remain source evidence and
    are never converted to base64 or submitted as documentation.
    Receipts and SHA claims are durable; ambiguous POSTs are never retried.
    Transport uses existing broker request types/policy and broker-held credentials.
    """
    def __init__(self, controller, bank, transport, *, max_polls=2, caller=None):
        if not isinstance(bank, str) or not bank or max_polls < 1:
            raise BoundaryError('bank and positive poll budget required')
        if 'ingestion' in controller._state:
            raise BoundaryError('ingestion already configured')
        self.controller, self.bank, self.transport = controller, bank, transport
        self.max_polls = max_polls
        # Legacy callers are scoped to their corpus, never to a shared default identity.
        self.caller = caller if caller is not None else str(controller.root)
        if not isinstance(self.caller, str) or not self.caller:
            raise BoundaryError('nonempty trusted caller required')
        self.run_id = controller._state['run_id']
        self.ledger_path = safe_path(controller.root, 'dedupe/controller-ingestion.sqlite3')
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = dict(bank=bank, caller=self.caller, artifacts={}, receipts={},
                          classifications={}, plan=copy.deepcopy(controller.plan), sources={})
        with self._ledger_lock():
            self._save()
        controller._state['ingestion'] = self.state

    @contextmanager
    def _ledger_lock(self):
        lock_path = safe_path(self.controller.root, 'dedupe/controller-ingestion.lock')
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            safe_path(self.controller.root, 'dedupe/controller-ingestion.sqlite3')
            ledger_fd = os.open(self.ledger_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(ledger_fd, 0o600)
            finally:
                os.close(ledger_fd)
            db = sqlite3.connect(self.ledger_path)
            self._db = db
            try:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('CREATE TABLE IF NOT EXISTS runs (caller TEXT, bank TEXT, run TEXT, state TEXT, PRIMARY KEY(caller,bank,run))')
                db.execute('CREATE TABLE IF NOT EXISTS claims (caller TEXT, bank TEXT, sha TEXT, receipt TEXT, PRIMARY KEY(caller,bank,sha))')
                row = db.execute('SELECT state FROM runs WHERE caller=? AND bank=? AND run=?',
                                 (self.caller, self.bank, self.run_id)).fetchone()
                if row:
                    self.state.clear()
                    self.state.update(json.loads(row[0]))
                    self.state.setdefault('classifications', {})
                    if any(self.controller.plan.get(key) != value
                           for key, value in self.state['plan'].items()):
                        raise BoundaryError('durable run plan binding mismatch')
                    self.state['plan'] = copy.deepcopy(self.controller.plan)
                    for source, receipt in self.state['sources'].items():
                        self.controller._state['sources'].setdefault(source, copy.deepcopy(receipt))
                    for identifier, receipt in self.state['receipts'].items():
                        claim = db.execute('SELECT receipt FROM claims WHERE caller=? AND bank=? AND sha=?',
                                           (self.caller, self.bank, receipt['sha256'])).fetchone()
                        if claim:
                            receipt.update(json.loads(claim[0]))
                            receipt.update(artifact_id=identifier, run_id=self.run_id)
                yield
            finally:
                db.close()
                self._db = None

    def _save(self):
        self.state['sources'] = copy.deepcopy(self.controller._state['sources'])
        self._db.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?,?)',
                         (self.caller, self.bank, self.run_id, json.dumps(self.state)))
        self._db.commit()

    def _save_receipt(self, record):
        self._db.execute('INSERT OR REPLACE INTO claims VALUES (?,?,?,?)',
                         (self.caller, self.bank, record['sha256'], json.dumps(record)))
        self._save()

    @_durable
    def prepare(self, source):
        from .acquisition_controller import read_safe
        from .document_payload_policy import (
            ASSET_ONLY, INGESTIBLE_TEXT, NEEDS_EXTRACTION, PayloadClassification,
            classify_payload,
        )
        receipt = self.controller._state['sources'].get(source, {})
        if receipt.get('status') != 'complete':
            raise BoundaryError('complete controller source required')
        identifiers = []
        for occurrence in receipt['occurrences']:
            data = read_safe(self.controller.root, occurrence['source_path'])
            if sha(data) != occurrence['sha256']:
                raise BoundaryError('controller source hash mismatch')
            plan = self.controller.plan.get(source, {})
            roots = plan.get('documentation_roots', [])
            path = occurrence.get('path')
            boundary = any(isinstance(path, str) and
                           (path == root or path.startswith(root + '/'))
                           for root in roots)
            classification = classify_payload(
                data, path=path, documentation_boundary=boundary)
            # Ubuntu acquisition records a verified extraction manifest. The
            # archive inventory itself is retained as source evidence, but is
            # not a PDF/text document and must not block the rest of the run.
            archive_inventory_verified = (
                plan.get('kind') == 'ubuntu-source'
                and plan.get('archive_manifest_sha256') == receipt.get('archive_manifest_sha256')
                and classification.classification == NEEDS_EXTRACTION
                and not data.startswith(b'%PDF-'))
            if archive_inventory_verified:
                classification = PayloadClassification(
                    ASSET_ONLY, 'verified-archive-inventory', classification.sha256,
                    classification.path, classification.mime_type)
            self.state['classifications'][occurrence['id']] = classification.as_dict()
            if classification.classification != INGESTIBLE_TEXT:
                continue
            text = data.decode('utf-8')
            representation = 'utf-8'
            # JSON quotes provenance as data; no repository templates or code run.
            import json
            provenance = json.dumps(dict(source_sha256=occurrence['sha256'], representation=representation), sort_keys=True)
            prepared = ('# Controller source artifact\n\n' + provenance + '\n\n' + text + '\n').encode()
            if len(prepared) > self.controller.max_bytes * 2:
                raise BoundaryError('prepared artifact size limit exceeded')
            identifier = sha(prepared)
            path = f'prepared/controller/{identifier}.md'
            try:
                self.controller._write(path, prepared)
            except FileExistsError:
                if read_safe(self.controller.root, path) != prepared:
                    raise BoundaryError('existing prepared artifact hash mismatch')
            artifact = self.state['artifacts'].setdefault(identifier, dict(
                id=identifier, path=path, sha256=identifier,
                occurrence_ids=[], provenance=[], representation=representation))
            if occurrence['id'] not in artifact['occurrence_ids']:
                artifact['occurrence_ids'].append(occurrence['id'])
                artifact['provenance'].append(copy.deepcopy(occurrence))
            if identifier not in identifiers:
                identifiers.append(identifier)
        self._save()
        return [copy.deepcopy(self.state['artifacts'][i]) for i in identifiers]

    def _artifact(self, identifier):
        from .acquisition_controller import read_safe
        if not isinstance(identifier, str) or identifier not in self.state['artifacts']:
            raise BoundaryError('unknown controller artifact ID')
        artifact = self.state['artifacts'][identifier]
        data = read_safe(self.controller.root, artifact['path'])
        if sha(data) != artifact['sha256']:
            raise BoundaryError('controller artifact hash mismatch')
        return artifact, data

    def _retention_context(self, artifact):
        """Build bounded provenance from controller-owned state only."""
        source_hashes = []
        provenance = []
        for occurrence in artifact.get('provenance', []):
            digest = occurrence.get('sha256')
            if isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest):
                if digest not in source_hashes:
                    source_hashes.append(digest)
            entry = {key: occurrence[key] for key in
                     ('id', 'path', 'source_url', 'repository_ref', 'blob', 'sha256')
                     if key in occurrence and isinstance(occurrence[key], str)}
            provenance.append(entry)
        plan_by_source = self.controller.plan
        source_ids = artifact.get('occurrence_ids', [])
        plans = [plan_by_source[source_id.split(':', 1)[0]] for source_id in source_ids
                 if source_id.split(':', 1)[0] in plan_by_source]
        source_receipts = self.controller._state.get('sources', {})
        receipts = [source_receipts[source_id.split(':', 1)[0]] for source_id in source_ids
                    if source_id.split(':', 1)[0] in source_receipts]
        product = next((x.get('product_id') for x in plans + receipts
                        if isinstance(x.get('product_id'), str)), None)
        version = next((x.get('version') for x in plans + receipts
                        if isinstance(x.get('version'), str)), None)
        context = dict(product=product, version=version, caller=self.caller,
                       bank=self.bank, source_sha256=source_hashes,
                       artifact_sha256=artifact['sha256'], provenance=provenance)
        encoded = json.dumps(context, sort_keys=True, separators=(',', ':'))
        if len(encoded.encode()) > 8192:
            raise BoundaryError('retention metadata limit exceeded')
        return context

    @_durable
    def submit(self, artifact_id):
        import json
        import secrets
        from .proxy import ProxyPolicy
        artifact, data = self._artifact(artifact_id)
        claim = self._db.execute('SELECT receipt FROM claims WHERE caller=? AND bank=? AND sha=?',
                                 (self.caller, self.bank, artifact['sha256'])).fetchone()
        if claim:
            record = json.loads(claim[0])
            record.update(artifact_id=artifact_id, run_id=self.run_id)
            self.state['receipts'][artifact_id] = record
            self._save()
            return copy.deepcopy(record)
        boundary = 'controller-' + secrets.token_hex(24)
        while boundary.encode() in data:
            boundary = 'controller-' + secrets.token_hex(24)
        context = self._retention_context(artifact)
        metadata = json.dumps({'files_metadata': [{'context': json.dumps(context, sort_keys=True,
            separators=(',', ':')), 'tags': documentation_tags(context),
            'strategy': DOC_RETAIN_STRATEGY}]}).encode()
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="request"\r\n'
                'Content-Type: application/json\r\n\r\n').encode() + metadata
        body += (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                 f'filename="{artifact_id}.md"\r\nContent-Type: text/markdown; charset=utf-8'
                 '\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
        record = dict(artifact_id=artifact_id, sha256=artifact['sha256'], bank=self.bank,
                      run_id=self.run_id, caller=self.caller, context=context,
                      status='unknown', polls=0)
        self.state['receipts'][artifact_id] = record
        self._save_receipt(record)  # Commit the content claim BEFORE any network I/O.
        try:
            response = self.transport(ProxyPolicy().request('retain', self.bank,
                body=body, content_type='multipart/form-data; boundary=' + boundary))
            record['http_status'] = response.status
            record['response_sha256'] = sha(response.body)
            result = json.loads(response.body)
            ids = result.get('operation_ids') if isinstance(result, dict) else None
            if (response.status not in (200, 202) or not isinstance(ids, list) or len(ids) != 1
                    or not isinstance(ids[0], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ids[0])
                    or any(json.loads(r[0]).get('operation_id') == ids[0] for r in
                           self._db.execute('SELECT receipt FROM claims WHERE caller=? AND bank=?',
                                            (self.caller, self.bank)))):
                raise BoundaryError('unverified retain response')
            record.update(status='accepted', operation_id=ids[0])
            document_ids = result.get('document_ids')
            if (isinstance(document_ids, list) and document_ids
                    and len(document_ids) <= 256
                    and all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', value)
                            for value in document_ids)):
                record['document_ids'] = list(document_ids)
            self._save_receipt(record)
        except Exception as exc:
            record['error'] = type(exc).__name__
        self._save_receipt(record)
        return copy.deepcopy(record)

    @_durable
    def poll(self, artifact_id):
        return self._poll(artifact_id)

    def _poll(self, artifact_id, *, resume=False):
        import json
        from .proxy import ProxyPolicy
        self._artifact(artifact_id)
        record = self.state['receipts'].get(artifact_id)
        if not record or 'operation_id' not in record:
            raise BoundaryError('no verified operation for artifact')
        if record['status'] in ('completed', 'failed', 'cancelled'):
            return copy.deepcopy(record)
        if not resume and record['polls'] >= self.max_polls:
            raise BoundaryError('poll budget exhausted')
        record['polls'] += 1
        try:
            response = self.transport(ProxyPolicy().request('operation_status', self.bank,
                operation_id=record['operation_id']))
            record.setdefault('checks', []).append(dict(http_status=response.status,
                response_sha256=sha(response.body)))
            result = json.loads(response.body)
            if (response.status != 200 or not isinstance(result, dict)
                    or result.get('status') not in ('pending', 'processing', 'completed', 'failed', 'cancelled')
                    or result.get('operation_id', record['operation_id']) != record['operation_id']):
                raise BoundaryError('unverified operation status')
            record['status'] = result['status']
        except Exception as exc:
            record['error'] = type(exc).__name__
        self._save_receipt(record)
        return copy.deepcopy(record)


    def _attach_durable_receipts(self):
        """Attach only verified in-flight claims from another run.

        A hash claim is sufficient to find the durable receipt, but not to
        promote an unknown or terminal disposition.  The source provenance in
        that receipt remains the original run's evidence.
        """
        attached = []
        for identifier, artifact in self.state['artifacts'].items():
            if identifier in self.state['receipts']:
                continue
            row = self._db.execute(
                'SELECT receipt FROM claims WHERE caller=? AND bank=? AND sha=?',
                (self.caller, self.bank, artifact['sha256'])).fetchone()
            if not row:
                continue
            try:
                record = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                continue
            if (not isinstance(record, dict)
                    or record.get('status') not in ('accepted', 'pending')
                    or not isinstance(record.get('operation_id'), str)
                    or not record['operation_id']):
                continue
            attached_record = copy.deepcopy(record)
            attached_record.update(artifact_id=identifier, run_id=self.run_id)
            self.state['receipts'][identifier] = attached_record
            attached.append(identifier)
        if attached:
            self._save()
        return attached

    def resume(self, *, max_submissions=16, max_status_checks=None):
        """One bounded controller-owned tick; repeat safely across process restarts.

        Unknown POST dispositions are returned for operator reconciliation, never
        resubmitted. Poll budgets are per tick, while receipt counters are lifetime.
        """
        if max_status_checks is None:
            max_status_checks = self.max_polls
        if any(type(n) is not int or not 0 <= n <= 256
               for n in (max_submissions, max_status_checks)):
            raise BoundaryError('batch budgets must be integers between 0 and 256')
        with self._ledger_lock():
            self._attach_durable_receipts()
            identifiers = list(self.state['artifacts'])
            missing = [i for i in identifiers if i not in self.state['receipts']]
        submitted = 0
        for identifier in missing[:max_submissions]:
            self.submit(identifier)
            submitted += 1
        checked = 0
        with self._ledger_lock():
            pending = sorted((r for r in self.state['receipts'].values()
                              if r['status'] not in ('completed', 'failed', 'cancelled', 'unknown')),
                             key=lambda r: r['polls'])
            for record in pending[:max_status_checks]:
                self._poll(record['artifact_id'], resume=True)
                checked += 1
            receipts = copy.deepcopy(self.state['receipts'])
            has_more = (len(receipts) < len(self.state['artifacts']) or any(
                r['status'] not in ('completed', 'failed', 'cancelled', 'unknown')
                for r in receipts.values()))
        return dict(submitted=submitted, checked=checked, has_more=has_more,
                    unknown_ids=[i for i, r in receipts.items() if r['status'] == 'unknown'],
                    receipts=receipts)


def validate_ingestion(trusted_evidence, contract, documents):
    """Additional completion gate; does not replace acquisition/report validation.

    Existing librarian_completion.validate_run intentionally remains fail-closed
    for ingestion until its owner explicitly wires this gate into that validator.
    Returns errors, never treats POST acceptance alone as completed ingestion.
    """
    from .acquisition_controller import TrustedEvidence, read_safe
    from .document_payload_policy import (
        ASSET_ONLY, classify_payload, INGESTIBLE_TEXT, NEEDS_EXTRACTION,
        PayloadClassification, UNRESOLVED,
    )
    if type(trusted_evidence) is not TrustedEvidence:
        return ['live trusted controller evidence required for ingestion']
    errors = []
    try:
        state = trusted_evidence.snapshot()
        ingestion = state['ingestion']
        if (state['run_id'] != contract['run_id'] or state['corpus_root'] != contract['corpus_root']
                or ingestion['bank'] != contract['hindsight_bank']):
            errors.append('ingestion run/corpus/bank binding mismatch')
        artifacts, receipts = ingestion['artifacts'], ingestion['receipts']
        if len(documents) != len(artifacts):
            errors.append('controller prepared artifact parity failed')
        expected = {(r['path'], r['sha256'], tuple(r['occurrence_ids'])) for r in artifacts.values()}
        actual = {(r['path'], r['sha256'], tuple(r['occurrence_ids'])) for r in documents}
        if expected != actual:
            errors.append('controller prepared provenance mismatch')
        acquired = {r['id'] for s in state['sources'].values() for r in s['occurrences']}
        prepared = {i for r in artifacts.values() for i in r['occurrence_ids']}
        classifications = ingestion.get('classifications', {})
        if (set(classifications) != acquired or set(state['plan']) != set(state['sources']) or any(
                s['status'] != 'complete' for s in state['sources'].values())):
            errors.append('ingestion does not cover complete source plan')
        for source_id, source in state['sources'].items():
            plan = state['plan'].get(source_id, {})
            roots = plan.get('documentation_roots', [])
            for occurrence in source.get('occurrences', []):
                data = read_safe(Path(state['corpus_root']), occurrence['source_path'])
                boundary = any(isinstance(occurrence.get('path'), str) and
                               (occurrence['path'] == root or occurrence['path'].startswith(root + '/'))
                               for root in roots)
                current = classify_payload(data, path=occurrence.get('path'),
                                           documentation_boundary=boundary).as_dict()
                if (plan.get('kind') == 'ubuntu-source'
                        and plan.get('archive_manifest_sha256') == source.get('archive_manifest_sha256')
                        and current['classification'] == NEEDS_EXTRACTION
                        and not data.startswith(b'%PDF-')):
                    current = PayloadClassification(
                        ASSET_ONLY, 'verified-archive-inventory', current['sha256'],
                        current['path'], current['mime_type']).as_dict()
                if classifications.get(occurrence['id']) != current:
                    errors.append('controller payload classification mismatch: ' + occurrence['id'])
                if current['classification'] in (NEEDS_EXTRACTION, UNRESOLVED):
                    errors.append('pending payload preparation: ' + occurrence['id'])
        if acquired != prepared | {
                i for i, c in classifications.items() if c.get('classification') != INGESTIBLE_TEXT}:
            errors.append('controller occurrence classification parity failed')
        if any(d.get('status') != 'resolved' for source in state['sources'].values()
               for d in source.get('dependencies', [])):
            errors.append('unresolved source dependencies prevent completion')
        for source_id, plan in state['plan'].items():
            if plan.get('kind') == 'ubuntu-source':
                source = state['sources'].get(source_id, {})
                if (source.get('archive_manifest_sha256') != plan.get('archive_manifest_sha256')
                        or 'dependencies' not in source):
                    errors.append('missing Ubuntu archive identity or dependency closure')
        for identifier, artifact in artifacts.items():
            if sha(read_safe(Path(state['corpus_root']), artifact['path'])) != artifact['sha256']:
                errors.append('controller prepared hash mismatch')
            receipt = receipts.get(identifier, {})
            if (receipt.get('status') != 'completed' or receipt.get('sha256') != artifact['sha256']
                    or receipt.get('bank') != contract['hindsight_bank']
                    or receipt.get('run_id') != contract['run_id']
                    or not receipt.get('checks')):
                errors.append('missing completed controller ingestion receipt: ' + identifier)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append('invalid ingestion evidence: ' + type(exc).__name__)
    return errors
