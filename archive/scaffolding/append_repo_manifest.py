import json, hashlib
from pathlib import Path
base=Path('/home/jsondarula/Liv-Home'); corpus=base/'Doc Corpus'; root=corpus/'reports/runs/acquire-router-fix-20260908T010000Z'; p=root/'manifest.json'; m=json.loads(p.read_text()); seen={x['path'] for x in m['files']}; d=root/'sources/ripgrep/14.1.0/repository-text'
for f in sorted(d.iterdir()):
    rel=str(f.relative_to(corpus))
    if f.is_file() and f.name!='SHA256SUMS' and rel not in seen:
        m['files'].append({'path':rel,'sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'canonical_url':'https://github.com/BurntSushi/ripgrep/blob/14.1.0/'+f.name.replace('__','/'),'mime_type':'text/plain'})
p.write_text(json.dumps(m,indent=2)+'\n'); print(len(m['files']))
