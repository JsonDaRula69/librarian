#!/usr/bin/env python3
import os,json,time,hashlib,requests
from pathlib import Path
from datetime import datetime,timezone
R=Path('/tmp/full-docs-backfill/google-gemini-workspace');m=json.loads((R/'source_manifest.json').read_text())
# Read only the named secret line; never serialize or print it.
key=None
for line in Path('/home/jsondarula/.hermes/.env').read_text().splitlines():
 if line.startswith('HINDSIGHT_API_KEY='):
  key=line.split('=',1)[1].strip().strip('"').strip("'");break
if not key: raise SystemExit('HINDSIGHT_API_KEY unavailable')
base='http://localhost:8888';h={'Authorization':f'Bearer {key}'}
audit_path=R/'hindsight_submission_audit.json'
audit=json.loads(audit_path.read_text()) if audit_path.exists() else {'run_utc':datetime.now(timezone.utc).isoformat(),'bank_name':'Liv','preflight':None,'batches':[]}
r=requests.get(base+'/v1/default/banks',headers=h,timeout=30);audit['preflight']={'http_status':r.status_code,'response_keys':sorted(r.json().keys()) if r.headers.get('content-type','').startswith('application/json') else []}
if r.status_code!=200: (R/'hindsight_submission_audit.json').write_text(json.dumps(audit,indent=2));raise SystemExit('bank preflight failed')
banks=r.json().get('banks',[]); bank=next((x for x in banks if x.get('name')=='Liv' or x.get('bank_id')=='Liv'),None)
if not bank: (R/'hindsight_submission_audit.json').write_text(json.dumps(audit,indent=2));raise SystemExit('Liv bank missing')
bankid=bank.get('bank_id','Liv');audit['bank_id']=bankid
selected=[]
for x in m['records']:
 if x['outcome']=='success' and x['file']:
  p=R/x['file']
  if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()==x['sha256']: selected.append((x,p))
  else: raise SystemExit('hash verification failed '+str(p))
done_urls={u for b in audit.get('batches',[]) if b.get('accepted') for u in b.get('urls',[])}
selected=[pair for pair in selected if pair[0]['source_url'] not in done_urls]
audit['new_selected_count']=len(selected)
# Submit conservative batches and persist audit after each accepted POST.
ops=[oid for b in audit.get('batches',[]) if b.get('accepted') for oid in b.get('operation_ids',[])]
for bi in range(0,len(selected),4):
 batch=selected[bi:bi+4];meta=[];files=[]
 try:
  for x,p in batch:
   files.append(('files',(p.name,p.open('rb'),'text/markdown' if p.suffix in ('.md','.rst') else 'text/html')))
   meta.append({'context':f"Official Google source: {x['source_url']}",'tags':['google-gemini-workspace','official','api-reference',x['category']]})
  req={'files_metadata':meta};resp=requests.post(base+f'/v1/default/banks/{bankid}/files/retain',headers=h,files=files,data={'request':json.dumps(req)},timeout=120)
  try: body=resp.json()
  except: body={}
  ids=body.get('operation_ids',[]) if isinstance(body,dict) else []
  ent={'batch_number':bi//4+1,'filenames':[p.name for _,p in batch],'urls':[x['source_url'] for x,_ in batch],'http_status':resp.status_code,'response_keys':sorted(body.keys()) if isinstance(body,dict) else [],'operation_ids':ids,'accepted':resp.status_code==200 and len(ids)==len(batch)}
  audit['batches'].append(ent);(R/'hindsight_submission_audit.json').write_text(json.dumps(audit,indent=2))
  if not ent['accepted']: raise SystemExit('submission rejected or operation mismatch')
  ops+=ids
 finally:
  for _,f in files: f[1].close()
audit['operation_count']=len(ops);(R/'hindsight_submission_audit.json').write_text(json.dumps(audit,indent=2))
# Bounded polling: three rounds, no service manipulation.
states={oid:None for oid in ops}; rounds=[]
for n in range(3):
 snap=[]
 for oid in ops:
  rr=requests.get(base+f'/v1/default/banks/{bankid}/operations/{oid}',headers=h,timeout=30)
  try: b=rr.json()
  except: b={}
  state=b.get('status') or b.get('state') or b.get('operation',{}).get('status')
  states[oid]=state
  snap.append({'operation_id':oid,'http_status':rr.status_code,'state':state,'response_keys':sorted(b.keys()) if isinstance(b,dict) else []})
 rounds.append({'round':n+1,'operations':snap})
 if all(str(s).lower() in {'completed','failed','cancelled','not_found'} for s in states.values()):break
 if n<2:time.sleep(5)
counts={k:sum(1 for v in states.values() if str(v).lower()==k) for k in ['completed','failed','cancelled','not_found']};counts['pending']=len(ops)-sum(counts.values())
operations={'operation_ids':ops,'rounds':rounds,'final_counts':counts,'recall':'skipped: operations not all successful terminal'}
# Recall only if every op completed.
if counts['completed']==len(ops):
 q={'query':'Gemini Enterprise Google Workspace API reference','tags':['google-gemini-workspace']}
 rr=requests.post(base+f'/v1/default/banks/{bankid}/memories/recall',headers=h,json=q,timeout=60)
 try:b=rr.json()
 except:b={}
 operations['recall']={'http_status':rr.status_code,'response_keys':sorted(b.keys()) if isinstance(b,dict) else [],'result_count':len(b.get('memories',b.get('results',[]))) if isinstance(b,dict) else None}
(R/'hindsight_operations.json').write_text(json.dumps(operations,indent=2))
# Evidence then immutable seal.
ledger=[]
for p in sorted(R.rglob('*')):
 if p.is_file():ledger.append({'path':str(p.relative_to(R)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size})
(R/'SHA256_LEDGER.json').write_text(json.dumps({'files':ledger},indent=2))
(R/'SEAL.json').write_text(json.dumps({'sealed_utc':datetime.now(timezone.utc).isoformat(),'classification':'bounded-incomplete: exact API reference/Gemini navigation and ADC SDK docs retained; official ADC narrative enumerator unavailable','selected':len(selected),'operations':len(ops),'operation_final_counts':counts},indent=2))
os.system("chmod -R a-w '/tmp/full-docs-backfill/google-gemini-workspace'")
print(json.dumps({'selected':len(selected),'operations':len(ops),'counts':counts,'sealed':True},indent=2))
