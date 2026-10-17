import json, subprocess, hashlib, shutil
from pathlib import Path
from datetime import datetime, timezone
ROOT=Path('/home/jsondarula/Liv-Home/Doc Corpus/sources/open-source/glances'); REPO=ROOT/'checkout'; SOURCES=ROOT/'corpus'
if REPO.exists(): shutil.rmtree(REPO)
subprocess.run(['git','clone','--filter=blob:none','https://github.com/nicolargo/glances.git',str(REPO)],check=True)
commit=subprocess.check_output(['git','-C',str(REPO),'rev-parse','HEAD'],text=True).strip()
origin=subprocess.check_output(['git','-C',str(REPO),'remote','get-url','origin'],text=True).strip()
# Bounded scope: authored Markdown/reST in upstream docs tree plus top-level README as official overview.
chosen=[]
for p in sorted((REPO/'docs').rglob('*')):
    if p.is_file() and p.suffix.lower() in ('.md','.rst'):
        chosen.append(p.relative_to(REPO).as_posix())
if (REPO/'README.md').is_file(): chosen.append('README.md')
SOURCES.mkdir(exist_ok=True)
records=[]
for n,rel in enumerate(chosen,1):
    src=REPO/rel; b=src.read_bytes(); status='downloaded'
    try: b.decode('utf-8'); utf8=True
    except UnicodeDecodeError: utf8=False; status='non_utf8_excluded'
    # preserve source suffix; deterministic prefixed names avoid collisions
    fn=f'{n:03d}--{rel.replace("/","--")}'; dst=SOURCES/fn
    if utf8 and b: dst.write_bytes(b)
    elif utf8: status='empty_excluded'
    records.append({'sequence':n,'repository_path':rel,'source_url':f'https://github.com/nicolargo/glances/blob/{commit}/{rel}','canonical_raw_url':f'https://raw.githubusercontent.com/nicolargo/glances/{commit}/{rel}','local_filename':str(Path('sources')/fn) if status=='downloaded' else None,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'utf8':utf8,'status':status})
manifest={'created_at':datetime.now(timezone.utc).isoformat(),'corpus':'official-glances-documentation','classification':'bounded-incomplete','index_result':'No validated official llms.txt or llms-full.txt at checked official documentation domains; see index-checks.json.','repository':{'origin':origin,'commit':commit,'canonical':'https://github.com/nicolargo/glances'},'selection_policy':{'included':'All authored .md and .rst files recursively under docs/, plus root README.md as official overview.','excluded':['All non-docs source code, tests/fixtures, dependencies/vendor trees, CI metadata, binaries/assets, generated build output, and potential secret-bearing configuration because the declared scope is official documentation only.'],'enumeration_count':len(chosen),'parity':'one manifest record per deterministic selection item'},'records':records}
(ROOT/'manifests'/'source-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
(ROOT/'reports'/'README-ingestion-scope.md').write_text(f'''# Glances official documentation ingestion\n\n- **Classification:** bounded-incomplete\n- **Canonical upstream:** https://github.com/nicolargo/glances at `{commit}`\n- **LLM-index check:** no valid `llms.txt`/`llms-full.txt` at the four official documentation URLs recorded in `index-checks.json`.\n- **Declared boundary:** all authored `.md`/`.rst` files below `docs/`, plus `README.md`; this is complete only within that declared GitHub documentation boundary, not all Glances vendor content.\n- **Selected records:** {len(chosen)}; download records and hashes are in `source-manifest.json`.\n''',encoding='utf-8')
print(json.dumps({'commit':commit,'enumerated':len(chosen),'downloaded':sum(r['status']=='downloaded' for r in records),'other':[(r['repository_path'],r['status']) for r in records if r['status']!='downloaded']},indent=2))
