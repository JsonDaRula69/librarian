#!/usr/bin/env python3
"""Offline allowlisted scratch image. Run with the pinned Hermes venv Python."""
import argparse
import hashlib
import importlib.metadata as metadata
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile


def _hermes_repo() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env) / "hermes-agent"
    try:
        return Path.home() / ".hermes" / "hermes-agent"
    except (RuntimeError, OSError):
        return Path(".hermes") / "hermes-agent"


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HERMES = Path(os.environ.get("HERMES_REPO", str(_hermes_repo())))
REVISION = '922c0d670cf46952248dc22c27e4d01531a4a051'
BASE = 'sha256:81ea5a19b1133dbad0726bfee4ddcfb66d2fb62dbe61ca2f05e8536074967c8e'
HERMES_FILES = '''agent/__init__.py agent/jiter_preload.py agent/lmstudio_reasoning.py
agent/reasoning_effort.py agent/moonshot_schema.py agent/prompt_builder.py
agent/runtime_cwd.py agent/skill_utils.py agent/secret_scope.py
agent/transports/__init__.py agent/transports/base.py agent/transports/types.py
agent/transports/chat_completions.py agent/transports/codex.py providers/__init__.py providers/base.py
tools/__init__.py tools/registry.py tools/threat_patterns.py tools/budget_config.py
hermes_constants.py utils.py'''.split()
PACKAGES = dict(tuple(item.split('==')) for item in '''PyYAML==6.0.3 openai==2.24.0
typing_extensions==4.16.0 tqdm==4.70.0 sniffio==1.3.1 pydantic==2.13.4
typing-inspection==0.4.4 pydantic_core==2.46.4 annotated-types==0.8.0 jiter==0.16.0
httpx==0.28.1 idna==3.18 httpcore==1.0.9 h11==0.16.0 certifi==2026.5.20
anyio==4.14.2 distro==1.9.0'''.split())
# This script runs in an existing immutable image, with no mounts or network.
# Only Python stdlib, its dynamic-link closure, Python and env leave the image.
EXPORT = r'''
import io, os, pathlib, re, subprocess, sys, tarfile
stdlib = pathlib.Path('/usr/local/lib/python3.11')
paths = {'/usr/local/bin/python3.11', '/usr/bin/env', '/usr/local/lib/libpython3.11.so.1.0', '/lib/x86_64-linux-gnu/libgcc_s.so.1', '/lib/x86_64-linux-gnu/librt.so.1', '/lib/x86_64-linux-gnu/libpthread.so.0', '/lib/x86_64-linux-gnu/libdl.so.2'}
for p in stdlib.rglob('*'):
    rel = p.relative_to(stdlib)
    if any(x in ('site-packages', '__pycache__', 'test', 'tests', 'idlelib', 'tkinter', 'ensurepip') for x in rel.parts): continue
    if p.name.startswith(('_test', '_xx', 'xxlimited', '_tkinter')): continue
    if p.is_file() and (p.suffix == '.py' or p.suffix == '.so'): paths.add(str(p))
for path in sorted(paths.copy()):
    if path.endswith('.so') or path in ('/usr/local/bin/python3.11', '/usr/bin/env', '/usr/local/lib/libpython3.11.so.1.0'):
        result = subprocess.run(['ldd', path], capture_output=True, text=True)
        if 'not found' in result.stdout: raise RuntimeError(result.stdout)
        for lib in re.findall(r'(/[^\s()]+)', result.stdout):
            if pathlib.Path(lib).is_file(): paths.add(os.path.normpath(lib))
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:
    for path in sorted(paths):
        data = pathlib.Path(path).read_bytes()
        item = tarfile.TarInfo(path.lstrip('/')); item.size = len(data)
        item.mode = 0o755 if os.access(path, os.X_OK) else 0o644
        tar.addfile(item, io.BytesIO(data))
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def collect():
    command = ['docker', 'run', '--rm', '--pull=never', '--network=none', '--read-only',
               '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user=65534:65534',
               '-i', BASE, '/usr/local/bin/python3.11', '-']
    result = subprocess.run(command, input=EXPORT.encode(), capture_output=True, check=True, timeout=120)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tar:
        for member in tar:
            if not member.isfile() or '..' in Path(member.name).parts or member.name.startswith('/'):
                raise ValueError('unsafe export')
            files[member.name] = (tar.extractfile(member).read(), member.mode)
    site = 'usr/local/lib/python3.11/site-packages/'
    for name, version in PACKAGES.items():
        dist = metadata.distribution(name)
        if dist.version != version:
            raise ValueError(f'{name}: expected {version}, found {dist.version}')
        for entry in dist.files or []:
            if '..' in entry.parts or '__pycache__' in entry.parts or entry.suffix == '.pyc':
                continue
            if entry.name in ('direct_url.json', 'INSTALLER', 'REQUESTED', 'RECORD'):
                continue
            if entry.suffix == '.pth' or entry.is_absolute():
                raise ValueError('unsafe package entry')
            path = Path(dist.locate_file(entry))
            if path.is_symlink() or not path.is_file():
                raise ValueError('non-regular package entry')
            data = path.read_bytes()
            if entry.hash:
                import base64
                actual = base64.urlsafe_b64encode(hashlib.new(entry.hash.mode, data).digest()).rstrip(b'=').decode()
                if actual != entry.hash.value:
                    raise ValueError(f'modified installed package: {entry}')
            files[site + str(entry)] = (data, 0o644)
    for path in HERMES_FILES:
        data = subprocess.check_output(['git', '-C', str(HERMES), 'show', REVISION + ':' + path])
        files[site + path] = (data, 0o644)
    files[site + 'broker/__init__.py'] = (b'', 0o644)
    for name in ('librarian_agent_client.py', 'librarian_request.py', 'librarian_identity.py'):
        path = ROOT / 'broker' / name
        if path.is_symlink():
            raise ValueError('symlink client forbidden')
        files[site + 'broker/' + name] = (path.read_bytes(), 0o644)
    files['usr/local/bin/python'] = files['usr/local/bin/python3.11']
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh-lock', action='store_true', help='explicitly approve new input hashes')
    args = parser.parse_args()
    files = collect()
    manifest = {'base_image': BASE, 'hermes_revision': REVISION, 'packages': PACKAGES,
                'files': {p: {'sha256': digest(data), 'size': len(data), 'mode': mode}
                          for p, (data, mode) in sorted(files.items())}}
    lock = HERE / 'inputs.lock.json'
    if args.refresh_lock:
        lock.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    elif json.loads(lock.read_text()) != manifest:
        raise ValueError('build inputs differ from lock; review before --refresh-lock')
    with tempfile.TemporaryDirectory(prefix='librarian-image-') as temp:
        context = Path(temp)
        with tarfile.open(context / 'rootfs.tar', 'w') as tar:
            for path, (data, mode) in sorted(files.items()):
                item = tarfile.TarInfo(path); item.size = len(data); item.mode = mode
                tar.addfile(item, io.BytesIO(data))
        (context / 'Dockerfile').write_bytes((HERE / 'Dockerfile').read_bytes())
        subprocess.run(['docker', 'build', '--network=none', '--pull=false',
                        '--iidfile', str(context / 'image-id'), str(context)], check=True, timeout=300)
        image = (context / 'image-id').read_text().strip()
        info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
        assert info['Id'] == image and not info['Config'].get('Volumes')
        report = {'image_id': image, 'size_bytes': info['Size'], 'file_count': len(files),
                  'inputs_lock_sha256': digest(lock.read_bytes()),
                  'dockerfile_sha256': digest((HERE / 'Dockerfile').read_bytes())}
        (HERE / 'image.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
