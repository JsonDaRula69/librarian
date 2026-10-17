import hashlib,json,time
from pathlib import Path
import requests
R=Path('/tmp/full-docs-backfill/openai-codex')
E=Path('/home/jsondarula/.hermes/.env')
key=None
for line in E.read_text().splitlines():
    if line.strip().startswith('HINDSIGHT_API_KEY='):
        key=line.split('=',1)[1].strip().strip('"').strip("'")
        break
if not key: raise SystemExit('key unavailable')
base='http://127.0.0.1:8888'; h={'Authorization':'Bearer '+key}
pre=requests.get(base+'/v1/default/banks',headers=h,timeout=30)
if pre.status_code!=200: raise SystemExit('preflight failed '+str(pre.status_code))
banks=pre.json().get('banks',pre.json().get('data',[]))
if not any(b.get('name')=='Liv' and b.get('bank_id',b.get('id'))=='Liv' for b in banks): raise SystemExit('Liv not found')
items=json.loads((R/'prepared_retention_manifest.json').read_text())
run='retain-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())
outdir=R/'retention'/run; outdir.mkdir(parents=True)
summary={'run_id':run,'endpoint':base,'bank_id':'Liv','preflight_http_status':pre.status_code,'batch_size':3,'total_prepared':len(items),'batches':[],'stopped':False}
for n,start in enumerate(range(0,len(items),3),1):
    batch=items[start:start+3]; handles=[]
    try:
        files=[]; meta=[]
        for it in batch:
            p=R/it['upload_file']; b=p.read_bytes()
            if hashlib.sha256(b).hexdigest()!=it['upload_sha256'] or not p.name.endswith('.md') or not b: raise RuntimeError('local artifact integrity failure')
            f=open(p,'rb'); handles.append(f); files.append(('files',(p.name,f,'text/markdown; charset=utf-8')))
            meta.append({'context':it['context'],'tags':it['tags']})
        res=requests.post(base+'/v1/default/banks/Liv/files/retain',headers=h,files=files,data={'request':json.dumps({'files_metadata':meta})},timeout=180)
        try: body=res.json()
        except Exception: body={'non_json_response':True}
        ops=body.get('operation_ids',[]) if isinstance(body,dict) else []
        accepted=res.status_code==200 and len(ops)==len(batch)
        rec={'batch':n,'filenames':[Path(x['upload_file']).name for x in batch],'source_urls':[x['source_url'] for x in batch],'http_status':res.status_code,'accepted':accepted,'accepted_operation_count':len(ops),'operation_ids':ops,'response':body if accepted else {'error_message':body.get('detail',body.get('message','response withheld')) if isinstance(body,dict) else 'non-json'}}
    except Exception as e:
        rec={'batch':n,'filenames':[Path(x['upload_file']).name for x in batch],'source_urls':[x['source_url'] for x in batch],'http_status':None,'accepted':False,'accepted_operation_count':0,'operation_ids':[],'error':type(e).__name__+': '+str(e)[:500]}
    finally:
        for f in handles: f.close()
    (outdir/f'batch-{n:03d}.json').write_text(json.dumps(rec,indent=2)+'\n')
    summary['batches'].append({'batch':n,'artifact':f'batch-{n:03d}.json','accepted':rec['accepted'],'operation_ids':rec['operation_ids'],'http_status':rec['http_status']})
    (outdir/'submission_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    if not rec['accepted']:
        summary['stopped']=True; break
summary['accepted_operations']=sum(len(x['operation_ids']) for x in summary['batches'])
summary['accepted_batches']=sum(x['accepted'] for x in summary['batches'])
(R/'retention'/run/'submission_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps({'run_id':run,'accepted_operations':summary['accepted_operations'],'accepted_batches':summary['accepted_batches'],'stopped':summary['stopped']}))
