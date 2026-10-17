"""Controller-owned acquisition, never an agent-report import API.

The caller MUST keep the controller, source plan and evidence handle outside the
agent's authority. Python object identity and same-UID files are NOT a security
boundary. Production needs a separate OS identity/container, read-only agent
mounts, a protected evidence channel and network egress policy (including DNS
rebinding protection). Do not deserialize an agent JSON file as trusted evidence.
This bounded controller copies Git blobs without checkout, filters, builds or
execution. Git plans enumerate the ENTIRE pinned tree, not a caller path subset.
Index plans cover the declared Markdown index only, not an entire website.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import lzma
from urllib.parse import urljoin, urlsplit, unquote


class BoundaryError(ValueError):
    pass


def safe_path(root: Path, value: str) -> Path:
    """Reject escapes and all existing symlink components, including root."""
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise BoundaryError('invalid relative path')
    parts = value.split('/')
    if any(p in ('', '.', '..') for p in parts) or PurePosixPath(value).is_absolute():
        raise BoundaryError('outside-root or noncanonical path')
    root = Path(root).absolute()
    path = root.joinpath(*parts)
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise BoundaryError('symlink path forbidden')
    if path.exists() and not path.is_file():
        raise BoundaryError('non-regular path forbidden')
    return path


def read_safe(root: Path, value: str) -> bytes:
    path = safe_path(root, value)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BoundaryError('non-regular or hardlinked file forbidden')
        return stream.read()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_ubuntu_plan(plan):
    """Validate the closed schema of a trusted resolver's archive inventory."""
    from .ubuntu_sources import API, fingerprint
    if (type(plan) is not dict or set(plan) != {'kind', 'product_id', 'version',
            'artifacts', 'publication', 'archive_manifest_sha256'}
            or plan['kind'] != 'ubuntu-source'
            or not isinstance(plan['product_id'], str)
            or not re.fullmatch(r'ubuntu/[a-z][a-z0-9-]*/[a-z0-9][a-z0-9+.-]*', plan['product_id'])
            or not isinstance(plan['version'], str)
            or not re.fullmatch(r'[0-9][A-Za-z0-9.+:~\-]*', plan['version'])
            or not isinstance(plan['publication'], str)
            or not re.fullmatch(re.escape(API) + r'/\+sourcepub/[0-9]+', plan['publication'])):
        raise BoundaryError('invalid Ubuntu source identity or plan fields')
    artifacts = plan['artifacts']
    if type(artifacts) is not list or not 2 <= len(artifacts) <= 100:
        raise BoundaryError('invalid Ubuntu artifact inventory')
    names = set()
    for artifact in artifacts:
        if (type(artifact) is not dict or set(artifact) != {'name', 'url', 'sha256', 'size'}
                or not isinstance(artifact['name'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+~_-]*', artifact['name'])
                or artifact['name'] in names or type(artifact['size']) is not int or artifact['size'] < 0
                or not isinstance(artifact['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', artifact['sha256'])
                or not isinstance(artifact['url'], str)):
            raise BoundaryError('invalid Ubuntu artifact record')
        names.add(artifact['name'])
        url = urlsplit(artifact['url'])
        if (url.scheme != 'https' or url.username or url.password or url.query or url.fragment
                or not (url.netloc in ('launchpad.net', 'launchpadlibrarian.net',
                    'archive.ubuntu.com', 'security.ubuntu.com', 'ports.ubuntu.com', 'old-releases.ubuntu.com')
                    or re.fullmatch(r'[a-z0-9-]+\.launchpadlibrarian\.net', url.netloc))
                or unquote(url.path.rsplit('/', 1)[-1]) != artifact['name']
                or '\\' in unquote(url.path)
                or any(p in ('.', '..') for p in unquote(url.path).split('/'))):
            raise BoundaryError('invalid Ubuntu artifact URL')
    if (sum(name.endswith('.dsc') for name in names) != 1
            or fingerprint(artifacts) != plan['archive_manifest_sha256']):
        raise BoundaryError('invalid Ubuntu archive manifest')


class TrustedEvidence:
    """An explicit trusted-caller input, not a credential or sandbox."""
    def __init__(self, controller):
        self._controller = controller

    def snapshot(self):
        return copy.deepcopy(self._controller._state)


class AcquisitionController:
    def __init__(self, run_id: str, corpus_root: Path, source_plan: dict, *,
                 allow_loopback=False, max_bytes=32 * 1024 * 1024, max_files=100000,
                 ubuntu_adapter=None):
        self.root = Path(corpus_root).absolute()
        safe_path(self.root, '.boundary-check')
        self.root.mkdir(parents=True, exist_ok=True)
        self.plan = copy.deepcopy(source_plan)
        self.allow_loopback = allow_loopback
        self.max_bytes = max_bytes
        self.max_files = max_files
        # This injection is a trusted transport seam, never an agent plan field.
        from .ubuntu_sources import UbuntuSourceAdapter
        self.ubuntu_adapter = ubuntu_adapter if ubuntu_adapter is not None else UbuntuSourceAdapter(
            max_file_bytes=max_bytes, max_files=max_files)
        self._state = {'schema': 'controller-evidence-v1', 'run_id': run_id,
                       'corpus_root': str(self.root), 'plan': copy.deepcopy(source_plan),
                       'sources': {}}
        for key in self.plan:
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', key):
                raise BoundaryError('invalid source ID')

    def evidence(self) -> TrustedEvidence:
        return TrustedEvidence(self)

    def _write(self, value, data):
        path = safe_path(self.root, value)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents overwrite of pre-existing agent files.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        return value

    def _git(self, repo, *args):
        env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LC_ALL': 'C',
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
               'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1',
               'GIT_NO_LAZY_FETCH': '1'}
        return subprocess.run(['git', '--no-replace-objects', '-C', str(repo),
                               '-c', 'core.hooksPath=/dev/null',
                               '-c', 'protocol.allow=never', *args],
                              env=env, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60).stdout

    def _tree(self, source, plan, receipt):
        pin = plan.get('commit', '')
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', pin):
            raise BoundaryError('full commit hash required')
        if set(plan) - {'kind', 'repository', 'commit'}:
            raise BoundaryError('Git path subsets or unknown plan fields forbidden')
        repo = Path(plan['repository'])
        if not repo.is_absolute() or not repo.is_dir():
            raise BoundaryError('Git repository must be a controller-managed local repository')
        if self._git(repo, 'cat-file', '-t', pin).strip() != b'commit':
            raise BoundaryError('pin must identify a commit, not a tree or tag')
        attempt = {'id': f'{source}:git-tree', 'method': 'git-ls-tree',
                   'repository_ref': f'{repo}@{pin}', 'status': 'failed'}
        receipt['attempts'].append(attempt)
        tree = self._git(repo, 'ls-tree', '-rz', '--full-tree', pin)
        attempt.update(status='succeeded', sha256=sha(tree))
        receipt['tree_sha256'] = sha(tree)
        receipt['commit'] = pin
        entries = []
        for line in tree.split(b'\0'):
            if not line:
                continue
            metadata, name = line.split(b'\t', 1)
            mode, kind, oid = metadata.decode('ascii').split()
            name = name.decode('utf-8', errors='strict')
            safe_path(self.root, name)
            entries.append({'path': name, 'blob': oid, 'mode': mode, 'kind': kind})
        if not entries or len(entries) > self.max_files:
            raise BoundaryError('empty tree or enumeration limit exceeded')
        receipt['entries'] = entries
        for entry in entries:
            if entry['kind'] != 'blob' or entry['mode'] not in ('100644', '100755'):
                raise BoundaryError('symlink or submodule requires separate approved acquisition')
            size = int(self._git(repo, 'cat-file', '-s', entry['blob']))
            if size > self.max_bytes:
                raise BoundaryError('blob size limit exceeded')
            data = self._git(repo, 'cat-file', 'blob', entry['blob'])
            algorithm = hashlib.sha1 if len(entry['blob']) == 40 else hashlib.sha256
            if algorithm(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != entry['blob']:
                raise BoundaryError('Git blob parity failed')
            location = self._write(f'sources/controller/{source}/files/{entry["path"]}', data)
            receipt['occurrences'].append({
                'id': f'{source}:{entry["path"]}', 'status': 'collected',
                'enumerator_ids': [source], 'source_path': location, 'sha256': sha(data),
                'path': entry['path'], 'blob': entry['blob'],
                'repository_ref': f'{repo}@{pin}:{entry["path"]}'})
        self._index(source, receipt)

    def _index(self, source, receipt):
        data = json.dumps({'generated_by': 'acquisition-controller',
                           'source': source, 'entries': receipt['entries']},
                          sort_keys=True, indent=2).encode()
        receipt['index_path'] = self._write(f'sources/controller/{source}/local-index.json', data)
        receipt['index_sha256'] = sha(data)

    def _url(self, url, boundary):
        parsed, allowed = urlsplit(url), urlsplit(boundary)
        decoded = unquote(parsed.path)
        if (parsed.scheme not in ('https', 'http') or parsed.username or parsed.password
                or parsed.fragment or parsed.query or '\\' in decoded
                or any(part in ('.', '..') for part in decoded.split('/'))
                or (parsed.scheme, parsed.netloc) != (allowed.scheme, allowed.netloc)
                or not decoded.startswith(unquote(allowed.path).rstrip('/') + '/')):
            raise BoundaryError('URL outside approved boundary')
        if parsed.scheme != 'https':
            raise BoundaryError('HTTPS required')
        from .safe_egress import validate_url, EgressError
        try:
            validate_url(url)
        except EgressError as exc:
            raise BoundaryError(str(exc)) from exc
        return url

    def _fetch(self, source, url, boundary, receipt):
        self._url(url, boundary)
        attempt = {'id': f'{source}:fetch:{len(receipt["attempts"])}',
                   'method': 'http-get', 'source_url': url, 'status': 'failed'}
        receipt['attempts'].append(attempt)
        from . import safe_egress
        try:
            response = safe_egress.fetch(url, max_bytes=self.max_bytes,
                                         validate=lambda target: self._url(target, boundary))
            data = response.data
            attempt.update(status='succeeded', http_status=response.status,
                           final_url=response.url, sha256=sha(data), bytes=len(data))
            if response.redirects:
                attempt['redirects'] = list(response.redirects)
            return data
        except (OSError, ValueError) as exc:
            attempt['error'] = f'{type(exc).__name__}: {exc}'
            if hasattr(exc, 'code'):
                attempt['http_status'] = exc.code
            raise

    def _fetch_index(self, source, plan, receipt):
        if set(plan) != {'kind', 'url', 'boundary'}:
            raise BoundaryError('index plan requires kind, url and boundary only')
        url, boundary = plan['url'], plan['boundary']
        data = self._fetch(source, url, boundary, receipt)
        receipt['upstream_index_path'] = self._write(f'sources/controller/{source}/upstream-index.txt', data)
        receipt['upstream_index_sha256'] = sha(data)
        text = data.decode('utf-8', errors='strict')
        if '<html' in text.lower() or '<!doctype' in text.lower():
            raise BoundaryError('HTML is not a Markdown index')
        links = sorted(set(urljoin(url, link) for link in re.findall(r'\[[^\]]*\]\(([^\s()]+)\)', text)))
        if not links or len(links) > self.max_files:
            raise BoundaryError('empty or oversized Markdown index')
        for link in links:
            self._url(link, boundary)
        receipt['entries'] = [{'source_url': link} for link in links]
        for i, link in enumerate(links):
            content = self._fetch(source, link, boundary, receipt)
            path = self._write(f'sources/controller/{source}/files/{i:06d}.txt', content)
            receipt['occurrences'].append({'id': f'{source}:{link}', 'status': 'collected',
                'enumerator_ids': [source], 'source_path': path, 'sha256': sha(content),
                'source_url': link})
        self._index(source, receipt)

    def acquire(self, source: str) -> dict:
        if source in self._state['sources']:
            raise BoundaryError('source already attempted; create a new run for retry')
        plan = self.plan[source]
        receipt = {'id': source, 'status': 'blocked', 'occurrences': [],
                   'entries': [], 'attempts': []}
        self._state['sources'][source] = receipt
        try:
            if plan['kind'] == 'git':
                self._tree(source, plan, receipt)
            elif plan['kind'] == 'index':
                self._fetch_index(source, plan, receipt)
            elif plan['kind'] == 'ubuntu-source':
                validate_ubuntu_plan(plan)
                receipt.update(product_id=plan['product_id'], version=plan['version'],
                               publication=plan['publication'])
                self.ubuntu_adapter.acquire(self, source, plan, receipt)
            else:
                raise BoundaryError('unsupported acquisition kind')
            receipt['status'] = 'complete'
        except (OSError, ValueError, KeyError, TypeError, EOFError, tarfile.TarError,
                lzma.LZMAError, subprocess.SubprocessError) as exc:
            receipt['status'] = 'partial' if receipt['occurrences'] else 'blocked'
            receipt['error'] = f'{type(exc).__name__}: {exc}'
        return copy.deepcopy(receipt)
