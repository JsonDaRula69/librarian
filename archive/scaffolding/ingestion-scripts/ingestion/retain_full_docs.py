import os,re,json,uuid,time,hashlib
from pathlib import Path
import requests
ROOT=Path('/tmp/full-docs-backfill'); BASE='http://127.0.0.1:8888'; BANK='Liv'
def key():
 s=Path('/home/jsondarula/.hermes/.env').read_text(); m=re.search(r'^HINDSIGHT_API_KEY\s*=\s*(.+?)\s*$',s,re.M)
 if not m: raise RuntimeError('HINDSIGHT_API_KEY not found')
 return m.group(1).strip().strip("'\"")
def safe_response(r):
 try: d=r.json()
 except Exception: d={'non_json':True}
 return {'http_status':r.status_code,'json_keys':list(d) if isinstance(d,dict) else None,'operation_ids':d.get('operation_ids') if isinstance(d,dict) else None,'error':d.get('detail') if isinstance(d,dict) else None}
def main():
 h={'Authorization':'Bearer '+key()}
 pre=requests.get(BASE+'/v1/default/banks',headers=h,timeout=30)
 if pre.status_code!=200: raise RuntimeError('preflight HTTP '+str(pre.status_code))
 data=pre.json(); banks=data.get('banks',[])
 if not any((x.get('name')==BANK or x.get('bank_id')==BANK) for x in banks): raise RuntimeError('Liv bank absent')
 for product in ('hindsight','ollama'):
  root=ROOT/product; manifest=json.loads((root/'source-manifest.json').read_text()); records=[x for x in manifest['sources'] if x['status'] in ('success','recovered')]
  run='retain-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:10]
  rd=root/'runs'/run; prep=rd/'prepared-markdown'; prep.mkdir(parents=True)
  submissions={'schema':'liv-retain-submissions-v1','run_id':run,'product':product,'base_url':BASE,'bank':BANK,'preflight':{'http_status':pre.status_code,'bank_found':True},'selected_count':len(records),'batches':[],'accepted_operation_ids':[],'created_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
  for i,r in enumerate(records):
   raw=(root/r['file']).read_bytes()
   if hashlib.sha256(raw).hexdigest()!=r['sha256']: raise RuntimeError('hash mismatch '+r['file'])
   text=raw.decode('utf-8')
   if not text.strip(): raise RuntimeError('empty '+r['file'])
   fn=f'{i+1:04d}_{Path(r["file"]).stem}.md'; (prep/fn).write_text(text,encoding='utf-8')
   r['prepared_file']=str((prep/fn).relative_to(root))
  for bi in range(0,len(records),2):
   subset=records[bi:bi+2]; handles=[]
   try:
    files=[]; metas=[]
    for r in subset:
     p=root/r['prepared_file']; fh=open(p,'rb'); handles.append(fh); files.append(('files',(p.name,fh,'text/markdown; charset=utf-8')))
     metas.append({'context':f'Official {product} documentation. Canonical source: {r["canonical_url"]}. Listed from official LLM index: {r["listed_url"]}.','tags':['official-documentation','full-docs-backfill',f'provider-{product}',f'corpus-full-docs-backfill-{product}',f'run-{run}']})
    response=requests.post(f'{BASE}/v1/default/banks/{BANK}/files/retain',headers=h,files=files,data={'request':json.dumps({'files_metadata':metas})},timeout=120)
   finally:
    for fh in handles: fh.close()
   sr=safe_response(response); batch={'batch_number':bi//2+1,'files':[{'prepared_file':r['prepared_file'],'canonical_url':r['canonical_url'],'sha256':r['sha256'],'tags':metas[j]['tags']} for j,r in enumerate(subset)],'response':sr}
   submissions['batches'].append(batch)
   ids=sr.get('operation_ids') or []
   if response.status_code!=200 or len(ids)!=len(subset):
    (rd/'liv-submissions.json').write_text(json.dumps(submissions,indent=2)+'\n'); raise RuntimeError(f'upload rejected at batch {batch["batch_number"]}: HTTP {response.status_code}, ids {len(ids)} expected {len(subset)}')
   submissions['accepted_operation_ids']+=ids
   (rd/'liv-submissions.json').write_text(json.dumps(submissions,indent=2)+'\n')
  print(product,run,'accepted',len(submissions['accepted_operation_ids']))
if __name__=='__main__': main()
