import hashlib,json,os
from pathlib import Path
R=Path('/tmp/full-docs-backfill/openai-codex')
records=json.loads((R/'source_manifest.json').read_text())
prepared=[]
(R/'upload').mkdir(exist_ok=True)
for x in records:
    if x['collection_status']=='unavailable': continue
    p=R/x['file']; b=p.read_bytes()
    assert len(b)==x['bytes'] and hashlib.sha256(b).hexdigest()==x['sha256'] and x['utf8_valid']
    name=f"{x['ordinal']:04d}_{x['sha256'][:16]}.md"
    q=R/'upload'/name
    q.write_bytes(b)
    assert q.read_bytes()==b
    prepared.append({'ordinal':x['ordinal'],'source_url':x['source_url'],'source_file':x['file'],'source_sha256':x['sha256'],'upload_file':'upload/'+name,'upload_sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b),'utf8_valid':True,'tags':['official-openai','openai-developers-codex','full-corpus-backfill'],'context':'Official OpenAI Developers/Codex documentation. Canonical listed source URL: '+x['source_url']})
coverage=json.loads((R/'coverage.json').read_text())
coverage['counts']['corpus_unique_url_identities']=len(records)
coverage['counts']['source_manifest_records']=len(records)
coverage['counts']['parity_ok']=len(records)==len({x['source_url'] for x in records})
coverage['counts']['valid_prepared_for_retention']=len(prepared)
coverage['parity_definition']='The corpus URL identity set is the deduplicated union of the explicitly selected logical index URLs and every URL listed by them. Source manifest has exactly one record per identity, including unavailable records.'
(R/'prepared_retention_manifest.json').write_text(json.dumps(prepared,indent=2)+'\n')
(R/'coverage.json').write_text(json.dumps(coverage,indent=2)+'\n')
print(json.dumps({'records':len(records),'valid_prepared':len(prepared),'unavailable':len(records)-len(prepared),'parity_ok':coverage['counts']['parity_ok']}))
