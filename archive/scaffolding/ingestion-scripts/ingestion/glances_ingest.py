import json, os, hashlib, time, sys
from pathlib import Path
from datetime import datetime, timezone
import requests

ROOT=Path('/home/jsondarula/Liv-Home/Doc Corpus/sources/open-source/glances')
manifest=json.loads((ROOT/'manifests'/'source-manifest.json').read_text())
run_id='liv-retain-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
RUN=ROOT/'reports'/'runs'/run_id; PREP=RUN/'prepared-markdown'; PREP.mkdir(parents=True)

def safe_write(name, obj):
    (RUN/name).write_text(json.dumps(obj,indent=2),encoding='utf-8')
def key_from_env(path):
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if line.strip().startswith('HINDSIGHT_API_KEY='):
            v=line.split('=',1)[1].strip()
            if len(v)>=2 and v[0] in "\"'" and v[-1]==v[0]: v=v[1:-1]
            return v
    raise RuntimeError('Authorized HINDSIGHT_API_KEY line absent from /home/jsondarula/.hermes/.env')
key=key_from_env('/home/jsondarula/.hermes/.env')
base='http://localhost:8888/v1/default'
s=requests.Session(); s.headers['Authorization']=f'Bearer {key}'
# preflight (no response body persisted to avoid accidental sensitive data)
r=s.get(base+'/banks',timeout=30)
pre={'timestamp':datetime.now(timezone.utc).isoformat(),'endpoint':base+'/banks','http_status':r.status_code,'response_keys':None,'matched_bank':None}
if r.status_code!=200:
    safe_write('liv-preflight.json',pre); print(json.dumps({'preflight':pre,'blocker':'bank preflight did not return HTTP 200'})); sys.exit(2)
try: data=r.json()
except Exception:
    safe_write('liv-preflight.json',pre); print(json.dumps({'preflight':pre,'blocker':'bank preflight response was not JSON'})); sys.exit(2)
pre['response_keys']=sorted(data.keys()) if isinstance(data,dict) else ['non_dict']
banks=data.get('banks',[]) if isinstance(data,dict) else []
for b in banks:
    if isinstance(b,dict) and b.get('name')=='Liv':
        pre['matched_bank']={'name':'Liv','bank_id':b.get('bank_id') or b.get('id') or 'Liv'}; break
safe_write('liv-preflight.json',pre)
if not pre['matched_bank']:
    print(json.dumps({'preflight':pre,'blocker':'Liv bank was absent from preflight'})); sys.exit(2)
bank_id=pre['matched_bank']['bank_id']
selected=[]
for rec in manifest['records']:
    if rec['status']!='downloaded': continue
    src=ROOT/'corpus'/Path(rec['local_filename']).name; b=src.read_bytes()
    if not b or hashlib.sha256(b).hexdigest()!=rec['sha256']:
        raise RuntimeError('manifest integrity failure: '+rec['repository_path'])
    txt=b.decode('utf-8')
    # Every upload artifact ends in .md, including safe copies of reST sources.
    out=PREP/(Path(rec['local_filename']).name + ('' if src.suffix.lower()=='.md' else '.md'))
    out.write_text(txt,encoding='utf-8')
    if not out.read_bytes() or out.suffix!='.md': raise RuntimeError('unsafe prepared file: '+out.name)
    item=dict(rec); item['prepared_filename']=str(out.relative_to(ROOT)); item['prepared_sha256']=hashlib.sha256(out.read_bytes()).hexdigest(); selected.append(item)
safe_write('prepared-manifest.json',{'run_id':run_id,'selected_count':len(selected),'items':selected})
sub={'run_id':run_id,'created_at':datetime.now(timezone.utc).isoformat(),'bank':pre['matched_bank'],'selected_count':len(selected),'batches':[],'stopped_on_error':False}
# Small 2-file batches, durable state saved after each acceptance/rejection.
for start in range(0,len(selected),2):
    items=selected[start:start+2]
    metas=[]; files=[]
    for it in items:
        p=ROOT/it['prepared_filename']
        metas.append({'context':f"Official Glances documentation, pinned upstream commit {manifest['repository']['commit']}. Source: {it['canonical_raw_url']}",'tags':['official-documentation','glances','glances-docs-2026','github-upstream','liv']})
        files.append(('files',(p.name,p.open('rb'),'text/markdown; charset=utf-8')))
    try:
        rr=s.post(f'{base}/banks/{bank_id}/files/retain',files=files,data={'request':json.dumps({'files_metadata':metas})},timeout=120)
        response={}
        try: response=rr.json()
        except Exception: pass
        ids=response.get('operation_ids') if isinstance(response,dict) else None
        batch={'batch_number':start//2+1,'filenames':[x['prepared_filename'] for x in items],'source_urls':[x['canonical_raw_url'] for x in items],'http_status':rr.status_code,'response_keys':sorted(response.keys()) if isinstance(response,dict) else [],'accepted':bool(rr.status_code==200 and isinstance(ids,list) and len(ids)==len(items)),'operation_ids':ids if isinstance(ids,list) else []}
    except Exception as e:
        batch={'batch_number':start//2+1,'filenames':[x['prepared_filename'] for x in items],'source_urls':[x['canonical_raw_url'] for x in items],'http_status':None,'accepted':False,'operation_ids':[],'error_type':type(e).__name__}
    finally:
        for _, tup in files: tup[1].close()
    sub['batches'].append(batch); safe_write('liv-submissions.json',sub)
    if not batch['accepted']:
        sub['stopped_on_error']=True; safe_write('liv-submissions.json',sub); break
ops=[]
for b in sub['batches']:
    for fn,url,op in zip(b['filenames'],b['source_urls'],b['operation_ids']): ops.append({'operation_id':op,'filename':fn,'source_url':url,'polls':[],'final_state':'pending'})
# Bounded two quick polls; no service restarts/reconfiguration.
for round_no in range(1,3):
    for op in ops:
        if op['final_state'] in ('completed','failed','cancelled','not_found'): continue
        try:
            rr=s.get(f"{base}/banks/{bank_id}/operations/{op['operation_id']}",timeout=30)
            d=rr.json() if rr.headers.get('content-type','').startswith('application/json') else {}
            state=(d.get('status') or d.get('state') or 'unknown') if isinstance(d,dict) else 'unknown'
            if rr.status_code==404: state='not_found'
            op['polls'].append({'round':round_no,'http_status':rr.status_code,'state':state,'response_keys':sorted(d.keys()) if isinstance(d,dict) else []})
            op['final_state']=state
        except Exception as e: op['polls'].append({'round':round_no,'http_status':None,'state':'poll_error','error_type':type(e).__name__}); op['final_state']='poll_error'
    safe_write('liv-operations.json',{'run_id':run_id,'poll_rounds_completed':round_no,'operations':ops})
    if round_no==1: time.sleep(2)
counts={}
for op in ops: counts[op['final_state']]=counts.get(op['final_state'],0)+1
summary={'run_id':run_id,'preflight_http':pre['http_status'],'bank_id':bank_id,'selected':len(selected),'accepted_operation_ids':len(ops),'submission_stopped_on_error':sub['stopped_on_error'],'operation_states':counts,'recall':'skipped: operations are nonterminal or submission was incomplete' if any(k not in ('completed','failed','cancelled','not_found') for k in counts) or sub['stopped_on_error'] else 'not_run'}
safe_write('run-summary.json',summary); print(json.dumps(summary,indent=2))
