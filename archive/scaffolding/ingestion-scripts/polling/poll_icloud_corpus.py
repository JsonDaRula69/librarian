import json,time,requests,concurrent.futures
from pathlib import Path
ROOT=Path('/tmp/full-docs-backfill/icloud-integration'); sub=json.loads((ROOT/'liv-submissions.json').read_text()); env=Path('/home/jsondarula/.hermes/.env'); key=None
for l in env.read_text().splitlines():
 if l.strip().startswith('HINDSIGHT_API_KEY='): key=l.split('=',1)[1].strip().strip('"\'');break
base='http://127.0.0.1:8888'; h={'Authorization':'Bearer '+key}; ops=[o for b in sub['batches'] for o in b['operation_ids']]; out={'operation_count':len(ops),'rounds':[]}; terminal={'completed','failed','cancelled','not_found'}
def check(o):
 try:
  r=requests.get(base+'/v1/default/banks/Liv/operations/'+o,headers=h,timeout=30)
  d=r.json() if r.headers.get('content-type','').startswith('application/json') else {}
  state=d.get('status',d.get('state',d.get('operation',{}).get('status','unknown'))) if isinstance(d,dict) else 'unknown'
  return o,{'http_status':r.status_code,'state':state}
 except Exception as e:return o,{'http_status':None,'state':'request_error','error':type(e).__name__}
last={}
for rn in range(1,4):
 with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex: last=dict(ex.map(check,ops))
 counts={}
 for v in last.values(): counts[v['state']]=counts.get(v['state'],0)+1
 out['rounds'].append({'round':rn,'checked_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'counts':counts,'operations':last})
 (ROOT/'liv-operations.json').write_text(json.dumps(out,indent=2))
 if all(v['state'] in terminal for v in last.values()):break
 time.sleep(5)
allterm=all(v['state'] in terminal for v in last.values()); good=all(v['state']=='completed' for v in last.values())
rec={'skipped':True,'reason':'operations not all successfully terminal'}
if allterm and good:
 q={'query':'imap.mail.me.com CalDAV read_only','tags':['corpus:icloud-integration']}
 r=requests.post(base+'/v1/default/banks/Liv/memories/recall',headers=h,json=q,timeout=60)
 try:d=r.json()
 except:d={}
 # no response source/bodies written
 rec={'skipped':False,'http_status':r.status_code,'result_count':len(d.get('memories',d.get('results',[]))) if isinstance(d,dict) else None,'response_keys':sorted(d.keys()) if isinstance(d,dict) else []}
(ROOT/'liv-recall.json').write_text(json.dumps(rec,indent=2))
print(json.dumps({'terminal':allterm,'successful_terminal':good,'counts':out['rounds'][-1]['counts'],'recall':rec}))
