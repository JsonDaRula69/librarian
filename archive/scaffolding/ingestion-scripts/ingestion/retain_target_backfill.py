#!/usr/bin/env python3
import hashlib,json,re,time,uuid
from pathlib import Path
import requests
ROOT=Path('/tmp/full-docs-backfill'); BASE='http://127.0.0.1:8888'; BANK='Liv'
def key():
 s=Path('/home/jsondarula/.hermes/.env').read_text(); m=re.search(r'^HINDSIGHT_API_KEY\s*=\s*(.+?)\s*$',s,re.M)
 if not m: raise RuntimeError('authorized HINDSIGHT_API_KEY line not found')
 return m.group(1).strip().strip("'\"")
def digest(b): return hashlib.sha256(b).hexdigest()
def main():
 h={'Authorization':'Bearer '+key()}; pre=requests.get(BASE+'/v1/default/banks',headers=h,timeout=30)
 if pre.status_code!=200: raise RuntimeError('Liv preflight HTTP '+str(pre.status_code))
 banks=pre.json().get('banks',[])
 if not any(x.get('name')==BANK or x.get('bank_id')==BANK for x in banks): raise RuntimeError('Liv bank missing')
 for corpus in ['opencode-core','sistersage']:
  root=ROOT/corpus; m=json.loads((root/'provenance-manifest.json').read_text())
  good=[x for x in m['sources'] if (x.get('status')=='success' or x.get('selection')=='included') and x.get('file')]
  run='retain-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:10]; rd=root/'runs'/run; prep=rd/'prepared-markdown'; prep.mkdir(parents=True)
  audit={'schema':'liv-retain-submissions-v2','corpus':corpus,'run_id':run,'bank':BANK,'base_url':BASE,'created_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'preflight':{'http_status':pre.status_code,'bank_found':True},'selected_count':len(good),'batches':[],'accepted_operation_ids':[]}
  prepared=[]
  for i,x in enumerate(good,1):
   raw=(root/x['file']).read_bytes()
   if digest(raw)!=x['sha256']: raise RuntimeError('hash mismatch '+x['file'])
   try: text=raw.decode('utf-8')
   except UnicodeDecodeError: raise RuntimeError('non-UTF8 '+x['file'])
   if not text.strip(): raise RuntimeError('empty '+x['file'])
   # Every input uses .md; retain original path/hash/source URL in wrapper for auditability.
   u=x.get('canonical_url') or x.get('source_url'); p=prep/f'{i:04d}_{Path(x["file"]).stem}.md'
   wrapper=f'# {corpus} source artifact\n\n- Original path: `{x.get("path",x["file"])}`\n- Canonical source: {u}\n- SHA-256: `{x["sha256"]}`\n- Inclusion: {x.get("reason","official sitemap documentation")}\n\n```text\n{text}\n```\n'
   p.write_text(wrapper,encoding='utf-8'); prepared.append((x,p,u))
  for start in range(0,len(prepared),5):
   group=prepared[start:start+5]; handles=[]
   try:
    files=[]; metas=[]
    for x,p,u in group:
     fh=p.open('rb');handles.append(fh);files.append(('files',(p.name,fh,'text/markdown; charset=utf-8')))
     metas.append({'context':f'First-party {corpus} full-corpus backfill. Canonical source: {u}. SHA-256: {x["sha256"]}.','tags':['official-documentation','full-docs-backfill',f'corpus-{corpus}',f'run-{run}']})
    r=requests.post(f'{BASE}/v1/default/banks/{BANK}/files/retain',headers=h,files=files,data={'request':json.dumps({'files_metadata':metas})},timeout=180)
   finally:
    for fh in handles:fh.close()
   try: d=r.json()
   except Exception:d={}
   safe={'http_status':r.status_code,'json_keys':list(d) if isinstance(d,dict) else [],'operation_ids':d.get('operation_ids') if isinstance(d,dict) else None,'detail':d.get('detail') if isinstance(d,dict) else None}
   batch={'batch_number':start//5+1,'files':[{'prepared_file':str(p.relative_to(root)),'source_url':u,'sha256':x['sha256']} for x,p,u in group],'response':safe};audit['batches'].append(batch)
   ids=safe['operation_ids'] or []
   if r.status_code!=200 or len(ids)!=len(group):
    (rd/'liv-submissions.json').write_text(json.dumps(audit,indent=2)+'\n');raise RuntimeError(f'upload failure batch {batch["batch_number"]}: HTTP {r.status_code}, operation ids={len(ids)}/{len(group)}')
   audit['accepted_operation_ids']+=ids;(rd/'liv-submissions.json').write_text(json.dumps(audit,indent=2)+'\n')
  print(corpus,run,len(audit['accepted_operation_ids']))
if __name__=='__main__':main()
