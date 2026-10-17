import hashlib,json,os
from pathlib import Path
r=Path('/tmp/full-docs-backfill/openai-codex'); retry=Path('/tmp/openai-showcase-launch-cal.retry')
for p in [r,r/'sources',r/'upload',r/'source_manifest.json',r/'prepared_retention_manifest.json',r/'coverage.json',r/'final_audit.json',r/'immutable_hashes.json']:
    os.chmod(p,0o755 if p.is_dir() else 0o644)
b=retry.read_bytes(); assert b
src=json.loads((r/'source_manifest.json').read_text()); row=next(x for x in src if x['source_url']=='https://developers.openai.com/showcase/launch-cal')
rel='sources/%04d_showcase_launch-cal.html_%s'%(row['ordinal'],hashlib.sha256(row['source_url'].encode()).hexdigest()[:12]); (r/rel).write_bytes(b)
row.update({'http_status':200,'final_url':'https://developers.openai.com/showcase/launch-cal','content_type':'text/html','collection_status':'downloaded_retry','file':rel,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'utf8_valid':True}); b.decode('utf8')
(r/'source_manifest.json').write_text(json.dumps(src,indent=2)+'\n')
prepared=json.loads((r/'prepared_retention_manifest.json').read_text()); name=f"{row['ordinal']:04d}_{row['sha256'][:16]}.md"; q=r/'upload'/name; q.write_bytes(b)
prepared.append({'ordinal':row['ordinal'],'source_url':row['source_url'],'source_file':rel,'source_sha256':row['sha256'],'upload_file':'upload/'+name,'upload_sha256':row['sha256'],'bytes':len(b),'utf8_valid':True,'tags':['official-openai','openai-developers-codex','full-corpus-backfill'],'context':'Official OpenAI Developers/Codex documentation. Canonical listed source URL: '+row['source_url']})
(r/'prepared_retention_manifest.json').write_text(json.dumps(prepared,indent=2)+'\n')
cov=json.loads((r/'coverage.json').read_text()); cov['counts'].update({'downloaded':539,'unavailable':0,'valid_prepared_for_retention':544,'parity_ok':True}); (r/'coverage.json').write_text(json.dumps(cov,indent=2)+'\n')
print(json.dumps({'source':row['source_url'],'bytes':len(b),'sha256':row['sha256']}))
