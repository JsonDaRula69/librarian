"""Trusted Ubuntu source adapter; no builds, patch execution or package installs.

Integration: policy._ubuntu calls resolve(product, version) -> (plan, proof).
Controller dispatches kind='ubuntu-source' to acquire(controller, source, plan,
receipt), then marks complete in its usual exception boundary. Coverage must
compare archive_manifest_sha256 rather than a Git commit and add every returned
unresolved dependency to its closure. Neither caller change is installed here.
Plans are controller-owned, never accepted from model-supplied JSON.
"""
from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import json
import lzma
import re
import tarfile
from urllib.parse import unquote, urlencode, urlsplit

from .acquisition_controller import BoundaryError
from .safe_egress import fetch as safe_fetch, validate_url


API = 'https://api.launchpad.net/1.0/ubuntu/+archive/primary'
DEPENDENCY_FIELDS = ('build-depends', 'build-depends-arch', 'build-depends-indep',
                     'depends', 'pre-depends', 'recommends', 'suggests',
                     'enhances', 'built-using', 'static-built-using')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def control_paragraphs(data):
    """Read Deb822 and clear-signed DSC payloads without claiming PGP validation."""
    text = data.decode('utf-8', errors='strict').replace('\r\n', '\n')
    if text.startswith('-----BEGIN PGP SIGNED MESSAGE-----\n'):
        try:
            text = text.split('\n\n', 1)[1].split('\n-----BEGIN PGP SIGNATURE-----', 1)[0]
        except IndexError as exc:
            raise BoundaryError('invalid clearsigned dsc') from exc
        text = '\n'.join(line[2:] if line.startswith('- ') else line for line in text.splitlines())
    paragraphs, fields, previous = [], {}, None
    for line in text.splitlines() + ['']:
        if not line.strip():
            if fields:
                paragraphs.append(fields)
            fields, previous = {}, None
        elif line.startswith('#'):
            continue
        elif line[0].isspace():
            if previous is None:
                raise BoundaryError('orphan Debian continuation')
            fields[previous] += '\n' + line.strip()
        else:
            name, separator, value = line.partition(':')
            name = name.lower()
            if not separator or not re.fullmatch(r'[a-z][a-z0-9-]*', name) or name in fields:
                raise BoundaryError('invalid or duplicate Debian field')
            fields[name], previous = value.strip(), name
    return paragraphs


def parse_dependencies(data, *, product, evidence):
    """Preserve every alternative/restriction/substvar; never select a version.

    Binary package names are NOT source identities. A subsequent Ubuntu binary
    publication resolver must map each applicable alternative to pinned sources.
    """
    rows = []
    for paragraph in control_paragraphs(data):
        for field in DEPENDENCY_FIELDS:
            for group_index, group in enumerate(paragraph.get(field, '').split(',')):
                if not group.strip():
                    continue
                for alternative_index, raw in enumerate(group.split('|')):
                    raw = raw.strip()
                    match = re.match(r'([a-z0-9][a-z0-9+.-]*)(?::([a-z0-9-]+))?(?=\s|\(|\[|<|$)', raw)
                    name = match.group(1) if match else raw
                    rows.append({'dependency_of': product, 'name': name,
                        'product_id': 'ubuntu-binary/' + product.split('/')[1] + '/' + name,
                        'version': 'unknown', 'requirement': raw, 'relation': field,
                        'group': group_index, 'alternative': alternative_index,
                        'binary_package': paragraph.get('package'),
                        'status': 'unresolved', 'reason': 'binary-to-source-resolution-required' if match
                            else 'debian-substitution-or-expression-unresolved',
                        'evidence': dict(evidence)})
    return rows


class UbuntuSourceAdapter:
    def __init__(self, *, fetch=None, max_artifact_bytes=128 * 1024 * 1024,
                 max_download_bytes=512 * 1024 * 1024,
                 max_expanded_bytes=512 * 1024 * 1024, max_file_bytes=32 * 1024 * 1024,
                 max_files=100000, max_artifacts=100, max_metadata_bytes=4 * 1024 * 1024,
                 max_pages=20):
        self.fetch = fetch or safe_fetch
        limits = (max_artifact_bytes, max_download_bytes, max_expanded_bytes,
                  max_file_bytes, max_files, max_artifacts, max_metadata_bytes, max_pages)
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError('limits must be positive integers')
        self.max_artifact_bytes = max_artifact_bytes
        self.max_download_bytes = max_download_bytes
        self.max_expanded_bytes = max_expanded_bytes
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.max_artifacts = max_artifacts
        self.max_metadata_bytes = max_metadata_bytes
        self.max_pages = max_pages

    def _get(self, url, limit, *, metadata=False):
        def validate(target):
            parsed, _ = validate_url(target)
            if metadata:
                allowed = parsed.netloc == 'api.launchpad.net' and parsed.path.startswith('/1.0/ubuntu/')
                if target != url:
                    raise BoundaryError('metadata redirect forbidden')
            else:
                allowed = (parsed.netloc in ('launchpad.net', 'launchpadlibrarian.net',
                    'archive.ubuntu.com', 'security.ubuntu.com', 'ports.ubuntu.com', 'old-releases.ubuntu.com')
                    or re.fullmatch(r'[a-z0-9-]+\.launchpadlibrarian\.net', parsed.netloc))
                if parsed.query:
                    allowed = False
            decoded = unquote(parsed.path)
            if (not allowed or '\\' in decoded or '\x00' in decoded
                    or any(part in ('.', '..') for part in decoded.split('/'))):
                raise BoundaryError('non-first-party Ubuntu URL')
        validate(url)
        result = self.fetch(url, max_bytes=limit, validate=validate)
        validate(result.url)
        for target in result.redirects:
            validate(target)
        if result.status != 200 or len(result.data) > limit:
            raise BoundaryError('invalid or oversized Ubuntu response')
        if metadata and result.redirects:
            raise BoundaryError('metadata redirect forbidden')
        return result.data

    def resolve(self, product, version):
        match = re.fullmatch(r'ubuntu/([a-z][a-z0-9-]*)/([a-z0-9][a-z0-9+.-]*)', product)
        if not match or not isinstance(version, str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+:~\-]*', version):
            raise BoundaryError('exact Ubuntu series/source/version required')
        series, package = match.groups()
        series_url = 'https://api.launchpad.net/1.0/ubuntu/' + series
        url = API + '?' + urlencode({'ws.op': 'getPublishedSources', 'source_name': package,
            'exact_match': 'true', 'version': version, 'distro_series': series_url})
        pages, publications, visited = [], [], set()
        while url:
            if url in visited or len(visited) >= self.max_pages:
                raise BoundaryError('publication pagination limit')
            visited.add(url)
            raw = self._get(url, self.max_metadata_bytes, metadata=True)
            pages.append({'url': url, 'sha256': digest(raw)})
            page = json.loads(raw)
            for row in page['entries']:
                if (row.get('source_package_name') == package and row.get('source_package_version') == version
                        and row.get('distro_series_link') == series_url):
                    link = row.get('self_link', '')
                    if not re.fullmatch(re.escape(API) + r'/\+sourcepub/[0-9]+', link):
                        raise BoundaryError('invalid Ubuntu publication link')
                    publications.append(link)
            url = page.get('next_collection_link')
        if not publications:
            raise BoundaryError('Ubuntu publication not verified')
        publication = sorted(set(publications))[0]
        files_url = publication + '?ws.op=sourceFileUrls'
        raw = self._get(files_url, self.max_metadata_bytes, metadata=True)
        pages.append({'url': files_url, 'sha256': digest(raw)})
        urls = json.loads(raw)
        if not isinstance(urls, list) or not urls or len(urls) > self.max_artifacts:
            raise BoundaryError('invalid source file inventory')
        by_name = {}
        for url in urls:
            name = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+~_-]*', name) or name in by_name:
                raise BoundaryError('invalid or duplicate artifact name')
            by_name[name] = url
        dsc_names = [name for name in by_name if name.endswith('.dsc')]
        if len(dsc_names) != 1:
            raise BoundaryError('exactly one dsc required')
        name = dsc_names[0]
        dsc = self._get(by_name[name], self.max_metadata_bytes)
        paragraphs = control_paragraphs(dsc)
        if len(paragraphs) != 1:
            raise BoundaryError('invalid dsc paragraphs')
        fields = paragraphs[0]
        if fields.get('source') != package or fields.get('version') != version:
            raise BoundaryError('dsc identity mismatch')
        artifacts = [{'name': name, 'url': by_name[name], 'sha256': digest(dsc), 'size': len(dsc)}]
        seen = {name}
        for line in fields.get('checksums-sha256', '').splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) != 3 or not re.fullmatch(r'[0-9a-fA-F]{64}', parts[0]) or not parts[1].isdigit():
                raise BoundaryError('invalid dsc SHA256 inventory')
            checksum, size, artifact = parts
            if artifact not in by_name or artifact in seen:
                raise BoundaryError('dsc inventory mismatch')
            seen.add(artifact)
            artifacts.append({'name': artifact, 'url': by_name[artifact], 'sha256': checksum.lower(), 'size': int(size)})
        if len(artifacts) < 2 or seen != set(by_name):
            raise BoundaryError('incomplete dsc SHA256 inventory')
        if any(a['size'] > self.max_artifact_bytes for a in artifacts) or sum(a['size'] for a in artifacts) > self.max_download_bytes:
            raise BoundaryError('source download limit')
        plan = {'kind': 'ubuntu-source', 'product_id': product, 'version': version,
                'artifacts': artifacts, 'publication': publication,
                'archive_manifest_sha256': fingerprint(artifacts)}
        proof = {'url': pages[0]['url'], 'sha256': pages[0]['sha256'], 'metadata': pages,
                 'publication': publication, 'dsc_sha256': digest(dsc),
                 'authentication': 'first-party-HTTPS; OpenPGP signature not verified'}
        return plan, proof

    def _expand(self, name, data, remaining):
        opener = gzip.open if name.endswith('.gz') else bz2.BZ2File if name.endswith('.bz2') else lzma.LZMAFile if name.endswith('.xz') else None
        if opener:
            with opener(io.BytesIO(data), 'rb') as stream:
                data = stream.read(remaining + 1)
        if len(data) > remaining:
            raise BoundaryError('archive expansion limit')
        return data

    def acquire(self, controller, source, plan, receipt):
        """Populate normal controller receipt; raise on any incomplete acquisition.

        Artifacts remain byte-identical, each tar has its own extracted namespace;
        Debian patches/diffs are retained, never applied. No merged build tree.
        """
        if plan.get('kind') != 'ubuntu-source' or fingerprint(plan['artifacts']) != plan['archive_manifest_sha256']:
            raise BoundaryError('invalid Ubuntu archive plan')
        artifacts = plan['artifacts']
        if (not artifacts or len(artifacts) > self.max_artifacts
                or any(type(a['size']) is not int or not 0 <= a['size'] <= self.max_artifact_bytes for a in artifacts)
                or sum(a['size'] for a in artifacts) > self.max_download_bytes):
            raise BoundaryError('source download limit')
        verified = []
        # Download and verify EVERYTHING before parsing archive content or writing.
        for artifact in artifacts:
            data = self._get(artifact['url'], self.max_artifact_bytes)
            if len(data) != artifact['size'] or digest(data) != artifact['sha256']:
                raise BoundaryError('artifact SHA256 or size mismatch')
            verified.append((artifact['name'], data))
            receipt['attempts'].append({'id': source + ':fetch:' + artifact['name'],
                'method': 'http-get', 'source_url': artifact['url'], 'status': 'succeeded',
                'sha256': digest(data), 'bytes': len(data)})
        outputs, dependencies, expanded, member_count = [], [], 0, 0
        for name, data in verified:
            outputs.append(('artifacts/' + name, data))
            if name.endswith('.dsc'):
                dependencies.extend(parse_dependencies(data, product=plan['product_id'],
                    evidence={'artifact': name, 'sha256': digest(data)}))
            if re.search(r'\.tar(?:\.(?:gz|bz2|xz))?$', name):
                raw = self._expand(name, data, self.max_expanded_bytes - expanded)
                expanded += len(raw)
                seen = set()
                with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as tar:
                    for member in tar:
                        member_count += 1
                        path = member.name.rstrip('/') if member.isdir() else member.name
                        if (not path or path.startswith('/') or '\\' in path or '\x00' in path
                                or any(p in ('', '.', '..') for p in path.split('/'))
                                or len(path) > 4096 or len(path.split('/')) > 100 or path in seen):
                            raise BoundaryError('unsafe or duplicate archive path')
                        seen.add(path)
                        if member_count > self.max_files or member.size > self.max_file_bytes:
                            raise BoundaryError('archive member limit')
                        if member.issym() or member.islnk() or not (member.isfile() or member.isdir()) or member.issparse():
                            raise BoundaryError('archive link/device/sparse member forbidden')
                        if member.isdir():
                            continue
                        stream = tar.extractfile(member)
                        content = stream.read(self.max_file_bytes + 1)
                        if len(content) != member.size:
                            raise BoundaryError('archive file size mismatch')
                        outputs.append(('extracted/' + name + '/' + path, content))
                        if path == 'debian/control' or path.endswith('/debian/control'):
                            dependencies.extend(parse_dependencies(content, product=plan['product_id'],
                                evidence={'artifact': name, 'path': path, 'sha256': digest(content)}))
            elif name.endswith('.diff.gz'):
                raw = self._expand(name, data, min(self.max_file_bytes, self.max_expanded_bytes - expanded))
                expanded += len(raw)
                outputs.append(('extracted/' + name + '/' + name[:-3], raw))
                dependencies.append({'dependency_of': plan['product_id'],
                    'name': 'patch-derived-dependency-inventory',
                    'product_id': plan['product_id'] + '/patch-derived-dependencies',
                    'version': 'unknown', 'status': 'unresolved',
                    'reason': 'patch-derived-dependency-inventory-unresolved',
                    'relation': 'debian-control',
                    'evidence': {'artifact': name, 'sha256': digest(data)}})
            elif not name.endswith(('.dsc', '.asc')):
                raise BoundaryError('unsupported source artifact format')
        if len(outputs) > self.max_files:
            raise BoundaryError('source file limit')
        for path, data in outputs:
            location = controller._write(f'sources/controller/{source}/files/{path}', data)
            receipt['entries'].append({'path': path, 'sha256': digest(data), 'bytes': len(data)})
            receipt['occurrences'].append({'id': source + ':' + path, 'status': 'collected',
                'enumerator_ids': [source], 'source_path': location, 'path': path, 'sha256': digest(data)})
        receipt['dependencies'] = dependencies
        receipt['archive_manifest_sha256'] = plan['archive_manifest_sha256']
        controller._index(source, receipt)
