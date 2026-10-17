import tarfile, hashlib
from pathlib import Path
base=Path('/home/jsondarula/Liv-Home'); root=base/'Doc Corpus/reports/runs/acquire-router-fix-20260908T010000Z'; src=root/'sources/ripgrep/14.1.0'; out=src/'repository-text'; out.mkdir(exist_ok=True)
with tarfile.open(src/'ripgrep-14.1.0.tar.gz') as t:
    names=[x for x in t.getnames() if x.rsplit('/',1)[-1].endswith(('.md','.txt','.yml','.yaml','.toml','.json','.rst','.tpl'))]
    for name in names:
        rel=name.split('ripgrep-14.1.0/',1)[1]; target=out/rel.replace('/','__'); target.write_bytes(t.extractfile(name).read())
with (out/'SHA256SUMS').open('w') as f:
    for p in sorted(out.iterdir()):
        if p.name!='SHA256SUMS': f.write(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n')
print(len(names))
