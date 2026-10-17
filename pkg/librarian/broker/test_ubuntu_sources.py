import hashlib
import io
import json
import tarfile
from urllib.parse import urlencode

from broker.acquisition_controller import AcquisitionController
from broker.safe_egress import FetchResult


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def fixture(archive_bytes=None):
    product, version = 'ubuntu/noble/bcmwl', '6.30.223.271+bdcom-0ubuntu10'
    api = 'https://api.launchpad.net/1.0/ubuntu/+archive/primary'
    query = api + '?' + urlencode({'ws.op': 'getPublishedSources', 'source_name': 'bcmwl',
        'exact_match': 'true', 'version': version,
        'distro_series': 'https://api.launchpad.net/1.0/ubuntu/noble'})
    publication = 'https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/123'
    base = 'https://launchpad.net/ubuntu/+archive/primary/+sourcefiles/bcmwl/' + version + '/'
    name = 'bcmwl_6.30.orig.tar.gz'
    payload = archive_bytes or archive([('bcmwl/README', b'docs'),
        ('bcmwl/debian/control', b'Source: bcmwl\nBuild-Depends: debhelper (>= 9), dkms | dkms-alt\n\nPackage: bcmwl-kernel-source\nDepends: ${misc:Depends}, linux-libc-dev:any (>= 1) [amd64] <!nocheck>\n'),
        ('bcmwl/debian/patches/fix.patch', b'patch')])
    dsc = (f'Format: 3.0 (quilt)\nSource: bcmwl\nVersion: {version}\nBuild-Depends: debhelper (>= 9), dkms | dkms-alt\nChecksums-Sha256:\n {hashlib.sha256(payload).hexdigest()} {len(payload)} {name}\n').encode()
    data = {query: json.dumps({'entries': [{'source_package_name': 'bcmwl',
        'source_package_version': version, 'distro_series_link': 'https://api.launchpad.net/1.0/ubuntu/noble',
        'self_link': publication}]}).encode(), publication + '?ws.op=sourceFileUrls':
        json.dumps([base + 'bcmwl.dsc', base + name]).encode(), base + 'bcmwl.dsc': dsc, base + name: payload}
    calls = []
    def fetch(url, *, max_bytes, validate):
        validate(url)
        calls.append(url)
        return FetchResult(data[url], url, 200, ())
    return product, version, data, calls, fetch


def test_full_source_acquisition(tmp_path):
    from broker.ubuntu_sources import UbuntuSourceAdapter
    product, version, data, calls, fetch = fixture()
    adapter = UbuntuSourceAdapter(fetch=fetch)
    plan, proof = adapter.resolve(product, version)
    controller = AcquisitionController('test', tmp_path, {'ubuntu': plan})
    receipt = {'entries': [], 'occurrences': [], 'attempts': []}
    adapter.acquire(controller, 'ubuntu', plan, receipt)
    paths = [entry['path'] for entry in receipt['entries']]
    assert any(path.endswith('README') for path in paths)
    assert any(path.endswith('fix.patch') for path in paths)
    assert any(path.endswith('.dsc') for path in paths)
    assert any(path.endswith('.tar.gz') for path in paths)
    dependencies = receipt['dependencies']
    assert {'debhelper', 'dkms', 'dkms-alt', 'linux-libc-dev'} <= {d['name'] for d in dependencies}
    assert any(d['name'] == '${misc:Depends}' for d in dependencies)
    assert all(d['status'] == 'unresolved' for d in dependencies)
    assert proof['sha256']
    assert receipt['archive_manifest_sha256'] == plan['archive_manifest_sha256']
    assert all((tmp_path / row['source_path']).is_file() for row in receipt['occurrences'])


import pytest
from broker.acquisition_controller import BoundaryError
from broker.ubuntu_sources import UbuntuSourceAdapter


def run_acquire(tmp_path, adapter, plan):
    controller = AcquisitionController('test', tmp_path, {'ubuntu': plan})
    receipt = {'entries': [], 'occurrences': [], 'attempts': []}
    adapter.acquire(controller, 'ubuntu', plan, receipt)
    return receipt


@pytest.mark.parametrize('name,kind', [('../escape', tarfile.REGTYPE),
    ('/escape', tarfile.REGTYPE), ('a/../../escape', tarfile.REGTYPE),
    ('link', tarfile.SYMTYPE), ('hard', tarfile.LNKTYPE),
    ('device', tarfile.CHRTYPE), ('fifo', tarfile.FIFOTYPE)])
def test_rejects_unsafe_archive_members(tmp_path, name, kind):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as tar:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = '/tmp/escape'
        tar.addfile(member)
    product, version, data, calls, fetch = fixture(stream.getvalue())
    adapter = UbuntuSourceAdapter(fetch=fetch)
    plan, _ = adapter.resolve(product, version)
    with pytest.raises(BoundaryError):
        run_acquire(tmp_path, adapter, plan)
    assert not list(tmp_path.rglob('*'))


def test_checks_hash_before_archive_parsing_or_writes(tmp_path):
    product, version, data, calls, fetch = fixture()
    adapter = UbuntuSourceAdapter(fetch=fetch)
    plan, _ = adapter.resolve(product, version)
    url = plan['artifacts'][1]['url']
    data[url] = b'X' * len(data[url])
    with pytest.raises(BoundaryError, match='SHA256'):
        run_acquire(tmp_path, adapter, plan)
    assert not list(tmp_path.rglob('*'))


@pytest.mark.parametrize('limits', [{'max_expanded_bytes': 100}, {'max_file_bytes': 1}, {'max_files': 1}])
def test_archive_resource_limits(tmp_path, limits):
    product, version, data, calls, fetch = fixture()
    adapter = UbuntuSourceAdapter(fetch=fetch, **limits)
    plan, _ = adapter.resolve(product, version)
    with pytest.raises(BoundaryError, match='limit'):
        run_acquire(tmp_path, adapter, plan)
    assert not list(tmp_path.rglob('*'))


def test_missing_sha256_is_not_accepted():
    product, version, data, calls, fetch = fixture()
    url = next(url for url in data if url.endswith('.dsc'))
    data[url] = data[url].split(b'Checksums-Sha256:')[0]
    with pytest.raises(BoundaryError, match='inventory'):
        UbuntuSourceAdapter(fetch=fetch).resolve(product, version)


def test_non_first_party_redirect_rejected():
    product, version, data, calls, fetch = fixture()
    def redirect(url, *, max_bytes, validate):
        result = fetch(url, max_bytes=max_bytes, validate=validate)
        if url.endswith('.dsc'):
            validate('https://attacker.example/archive.dsc')
        return result
    with pytest.raises(BoundaryError, match='first-party'):
        UbuntuSourceAdapter(fetch=redirect).resolve(product, version)


def test_unsupported_archive_compression_blocks_completion(tmp_path):
    product, version, data, calls, fetch = fixture()
    old = next(url for url in data if url.endswith('.tar.gz'))
    new = old.replace('.tar.gz', '.tar.zst')
    data[new] = data.pop(old)
    for url in list(data):
        if url.endswith('.dsc') or url.endswith('sourceFileUrls'):
            data[url] = data[url].replace(b'.tar.gz', b'.tar.zst')
    adapter = UbuntuSourceAdapter(fetch=fetch)
    plan, _ = adapter.resolve(product, version)
    with pytest.raises(BoundaryError, match='unsupported source artifact'):
        run_acquire(tmp_path, adapter, plan)
    assert not list(tmp_path.rglob('*'))


def test_legacy_diff_records_unresolved_dependency_inventory(tmp_path):
    import gzip
    product, version, data, calls, fetch = fixture()
    old = next(url for url in data if url.endswith('.tar.gz'))
    new = old.replace('.orig.tar.gz', '.diff.gz')
    original = data.pop(old)
    payload = gzip.compress(b'--- a/debian/control\n+++ b/debian/control\n+Depends: dkms\n')
    data[new] = payload
    for url in list(data):
        if url.endswith('.dsc') or url.endswith('sourceFileUrls'):
            data[url] = data[url].replace(b'.orig.tar.gz', b'.diff.gz')
        if url.endswith('.dsc'):
            data[url] = data[url].replace(hashlib.sha256(original).hexdigest().encode(), hashlib.sha256(payload).hexdigest().encode()).replace(str(len(original)).encode() + b' ', str(len(payload)).encode() + b' ')
    adapter = UbuntuSourceAdapter(fetch=fetch)
    plan, _ = adapter.resolve(product, version)
    receipt = run_acquire(tmp_path, adapter, plan)
    assert any(row['reason'] == 'patch-derived-dependency-inventory-unresolved' for row in receipt['dependencies'])
