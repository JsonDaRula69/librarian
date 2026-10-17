import json, hashlib
from pathlib import Path
base=Path('/home/jsondarula/Liv-Home'); corpus=base/'Doc Corpus'; root=corpus/'reports/runs/acquire-router-fix-20260908T010000Z'; p=root/'manifest.json'; m=json.loads(p.read_text()); seen={x['path'] for x in m['files']}; d=root/'sources/ripgrep/14.1.0/dependencies/external'; versions={'anyhow':'1.0.79','bstr':'1.9.0','lexopt':'0.3.0','log':'0.4.20','pcre2':'0.2.6','pcre2-sys':'0.2.8','serde_json':'1.0.111','termcolor':'1.4.0','textwrap':'0.16.0'}
for name,v in versions.items():
 f=d/(name+'-'+v+'.html'); rel=str(f.relative_to(corpus))
 if f.exists() and rel not in seen: m['files'].append({'path':rel,'sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'canonical_url':f'https://docs.rs/crate/{name}/{v}','mime_type':'text/html'})
p.write_text(json.dumps(m,indent=2)+'\n'); print(len(m['files']))
