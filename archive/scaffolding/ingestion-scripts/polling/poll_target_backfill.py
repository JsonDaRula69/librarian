#!/usr/bin/env python3
import json,re,time
from pathlib import Path
import requests
ROOT=Path('/tmp/full-docs-backfill');BASE='http://127.0.0.1:8888';BANK='Liv';TERMINAL={'completed','failed','cancelled','not_found'}
def key():
 s=Path('/home/jsondarula/.hermes/.env').read_text();return re.search(r'^HINDSIGHT_API_KEY\s*=\s*(.+?)\s*$',s,re.M).group(1).strip().strip("'\"")
def main():
 h={'Authorization':'Bearer '+key()}
 for corpus in ['opencode-core','sistersage']:
  root=ROOT/corpus; rd=sorted((root/'runs').iterdir())[-1]; sub=json.loads((rd/'liv-submissions.json').read_text());ids=sub['accepted_operation_ids']; latest={}
  out={'schema':'liv-operations-v2','corpus':corpus,'run_id':sub['run_id'],'bank':BANK,'operation_count':len(ids),'poll_rounds':[],'final':{},'recall':None}
  for n in range(1,4):
   rows=[]
   for oid in ids:
    r=requests.get(f'{BASE}/v1/default/banks/{BANK}/operations/{oid}',headers=h,timeout=20)
    try:d=r.json()
    except Exception:d={}
    st=d.get('status') if r.status_code==200 else ('not_found' if r.status_code==404 else 'http_error')
    latest[oid]={'operation_id':oid,'http_status':r.status_code,'state':st};rows.append(latest[oid])
   counts={s:sum(x['state']==s for x in rows) for s in sorted(set(x['state'] for x in rows))};out['poll_rounds'].append({'round':n,'at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'counts':counts,'operations':rows})
   (rd/'liv-operations.json').write_text(json.dumps(out,indent=2)+'\n')
   if all(x['state'] in TERMINAL for x in rows):break
   time.sleep(5)
  counts={s:sum(x['state']==s for x in latest.values()) for s in sorted(set(x['state'] for x in latest.values()))}
  all_ok=len(latest)==len(ids) and all(x['state']=='completed' for x in latest.values());out['final']={'counts':counts,'all_terminal':all(x['state'] in TERMINAL for x in latest.values()),'all_succeeded':all_ok}
  if all_ok:
   r=requests.post(f'{BASE}/v1/default/banks/{BANK}/memories/recall',headers={**h,'Content-Type':'application/json'},json={'query':'OpenCode configuration plugin operation behavior' if corpus=='opencode-core' else 'SisterSage plugin operation behavior','filters':{'tags':[f'corpus-{corpus}']}},timeout=45)
   try:d=r.json()
   except Exception:d={}
   out['recall']={'attempted':True,'http_status':r.status_code,'result_count':len(d.get('memories',d.get('results',[]))) if isinstance(d,dict) else None,'json_keys':list(d) if isinstance(d,dict) else []}
  else:out['recall']={'attempted':False,'reason':'skipped: bounded polling left nonterminal or non-success operations'}
  (rd/'liv-operations.json').write_text(json.dumps(out,indent=2)+'\n');print(corpus,out['final'],out['recall'])
if __name__=='__main__':main()
