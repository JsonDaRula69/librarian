import json,os,hashlib
from pathlib import Path
r=Path('/tmp/full-docs-backfill/openai-codex')
for p in [r,r/'coverage.json',r/'final_audit.json',r/'immutable_hashes.json']:
 os.chmod(p,0o755 if p.is_dir() else 0o644)
c=json.loads((r/'coverage.json').read_text()); u=list(dict.fromkeys(c['corpus_definition']['logical_index_urls'])); c['corpus_definition']['logical_index_urls']=u;c['counts']['logical_indexes']=len(u);(r/'coverage.json').write_text(json.dumps(c,indent=2)+'\n')
f=json.loads((r/'final_audit.json').read_text());f['coverage']=c['counts'];(r/'final_audit.json').write_text(json.dumps(f,indent=2)+'\n')
items=[]
for p in sorted(r.rglob('*')):
 if p.is_file() and p.name!='immutable_hashes.json':items.append({'path':str(p.relative_to(r)),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
(r/'immutable_hashes.json').write_text(json.dumps({'policy':'Read-only audit snapshot. Immutable source content is SHA-256 pinned in source_manifest.json; this inventory pins all corpus artifacts generated before lock.','files':items},indent=2)+'\n')
for p in sorted(r.rglob('*'),reverse=True):os.chmod(p,0o444 if p.is_file() else 0o555)
os.chmod(r,0o555);print({'unique_index_urls':len(u),'root_mode':oct(os.stat(r).st_mode&0o777)})
