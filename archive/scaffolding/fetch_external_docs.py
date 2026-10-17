import urllib.request
from pathlib import Path
base=Path('/home/jsondarula/Liv-Home/Doc Corpus/reports/runs/acquire-router-fix-20260908T010000Z/sources/ripgrep/14.1.0/dependencies/external')
base.mkdir(exist_ok=True)
items=[('anyhow','1.0.79'),('bstr','1.9.0'),('lexopt','0.3.0'),('log','0.4.20'),('pcre2','0.2.6'),('pcre2-sys','0.2.8'),('serde_json','1.0.111'),('termcolor','1.4.0'),('textwrap','0.16.0')]
for name,version in items:
    url=f'https://docs.rs/crate/{name}/{version}'
    try: (base/f'{name}-{version}.html').write_bytes(urllib.request.urlopen(url,timeout=30).read())
    except Exception as e: (base/f'{name}-{version}.failure.txt').write_text(f'{url}\n{type(e).__name__}: {e}\n')
print(len(list(base.iterdir())))
