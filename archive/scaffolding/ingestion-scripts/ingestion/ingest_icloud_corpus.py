import os,json,time,requests,sys
from pathlib import Path
ROOT=Path('/tmp/full-docs-backfill/icloud-integration'); M=json.loads((ROOT/'manifest.json').read_text()); env=Path('/home/jsondarula/.hermes/.env')
key=None
for l in env.read_text().splitlines():
 if l.strip().startswith('HINDSIGHT_API_KEY='): key=l.split('=',1)[1].strip().strip('"\''); break
if not key: raise SystemExit('HINDSIGHT_API_KEY unavailable')
h={'Authorization':'Bearer '+key}; base='http://127.0.0.1:8888'; r=requests.get(base+'/v1/default/banks',headers=h,timeout=30)
if r.status_code!=200: raise SystemExit('bank preflight non-200')
banks=r.json().get('banks',r.json().get('data',[]));
if not any(b.get('bank_id',b.get('id'))=='Liv' or b.get('name')=='Liv' for b in banks): raise SystemExit('Liv bank absent')
items=[x for x in M['records'] if x.get('valid') and x.get('upload_path')]
audit={'preflight':{'http_status':r.status_code,'bank':'Liv'},'batch_size':4,'items':len(items),'batches':[],'stopped':False}
ap=ROOT/'liv-submissions.json'
def save(): ap.write_text(json.dumps(audit,indent=2))
save()
for start in range(0,len(items),4):
 batch=items[start:start+4]; files=[]
 try:
  metas=[]
  for x in batch:
   f=open(ROOT/x['upload_path'],'rb'); files.append(('files',(Path(x['upload_path']).name,f,'text/markdown')))
   metas.append({'context':'Official iCloud integration corpus source: '+x['final_url'],'tags':['corpus:icloud-integration','provider:apple-or-official-tool','topic:icloud-mail-calendar-contacts']})
  rr=requests.post(base+'/v1/default/banks/Liv/files/retain',headers=h,files=files,data={'request':json.dumps({'files_metadata':metas})},timeout=180)
  try: body=rr.json()
  except: body={}
 finally:
  for _,(_,f,_) in files: f.close()
 ops=body.get('operation_ids',[]) if isinstance(body,dict) else []
 row={'batch':start//4+1,'filenames':[Path(x['upload_path']).name for x in batch],'urls':[x['final_url'] for x in batch],'http_status':rr.status_code,'accepted':rr.status_code==200 and len(ops)==len(batch),'operation_ids':ops,'response_keys':sorted(body.keys()) if isinstance(body,dict) else []}
 audit['batches'].append(row); save()
 if not row['accepted']:
  audit['stopped']=True; audit['stop_reason']='non-200 or operation count mismatch'; save(); print(json.dumps({'accepted':sum(len(x['operation_ids']) for x in audit['batches']),'failed_batch':row['batch'],'status':rr.status_code})); sys.exit(2)
print(json.dumps({'accepted':sum(len(x['operation_ids']) for x in audit['batches']),'batches':len(audit['batches'])}))
