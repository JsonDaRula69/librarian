import concurrent.futures,json,time
from pathlib import Path
import requests
R=Path('/tmp/full-docs-backfill/openai-codex')
summary_path=next((R/'retention').glob('*/submission_summary.json'))
sub=json.loads(summary_path.read_text()); opids=[z for b in sub['batches'] for z in b['operation_ids']]
E=Path('/home/jsondarula/.hermes/.env'); key=None
for line in E.read_text().splitlines():
    if line.strip().startswith('HINDSIGHT_API_KEY='): key=line.split('=',1)[1].strip().strip('"').strip("'"); break
if not key: raise SystemExit('key unavailable')
base='http://127.0.0.1:8888'; h={'Authorization':'Bearer '+key}; outdir=summary_path.parent
history={op:[] for op in opids}; terminal={'completed','failed','cancelled','not_found'}
def check(op):
    try:
        r=requests.get(base+'/v1/default/banks/Liv/operations/'+op,headers=h,timeout=30)
        j=r.json() if r.headers.get('Content-Type','').startswith('application/json') else {}
        return op,{'http_status':r.status_code,'status':j.get('status','not_found' if r.status_code==404 else None),'operation_type':j.get('operation_type'),'error_message':j.get('error_message')}
    except Exception as e:return op,{'http_status':None,'status':None,'error_message':type(e).__name__+': '+str(e)[:200]}
for rnd in range(1,3):
    active=[op for op in opids if not history[op] or history[op][-1]['status'] not in terminal]
    if not active: break
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        for op,r in ex.map(check,active): r['poll']=rnd; history[op].append(r)
    if rnd<2 and any(history[x][-1]['status'] not in terminal for x in active): time.sleep(3)
final={op:h[-1] if h else {} for op,h in history.items()}
counts={s:sum(x.get('status')==s for x in final.values()) for s in ['completed','failed','cancelled','not_found','pending']}
counts['other']=len(final)-sum(counts.values())
ops={'run_id':sub['run_id'],'bounded_poll_rounds_executed':max((len(x) for x in history.values()),default=0),'operations':history,'final_counts':counts}
(outdir/'operation_statuses.json').write_text(json.dumps(ops,indent=2)+'\n')
recall={'condition':'Recall attempted only if every accepted operation was terminal and none failed/cancelled.' ,'attempted':False}
if counts['pending']==0 and counts['other']==0 and counts['failed']==0 and counts['cancelled']==0 and counts['not_found']==0:
    recall['attempted']=True
    payload={'query':'OpenAI Codex configuration reference','tags':['official-openai','openai-developers-codex','full-corpus-backfill']}
    try:
        rr=requests.post(base+'/v1/default/banks/Liv/memories/recall',headers=h,json=payload,timeout=60)
        recall.update({'request_shape':'tag-scoped','http_status':rr.status_code})
        try: body=rr.json(); recall['result_count']=len(body.get('memories',body.get('results',body.get('data',[])))) if isinstance(body,dict) else 0; recall['response_schema_keys']=list(body) if isinstance(body,dict) else []
        except Exception:recall['result_count']=None;recall['response_schema_keys']=[]
        if rr.status_code>=400:
            payload={'query':'OpenAI Codex configuration reference'}; rr=requests.post(base+'/v1/default/banks/Liv/memories/recall',headers=h,json=payload,timeout=60)
            recall.update({'fallback_request_shape':'query-only','fallback_http_status':rr.status_code})
            try: body=rr.json(); recall['fallback_result_count']=len(body.get('memories',body.get('results',body.get('data',[])))) if isinstance(body,dict) else 0
            except Exception: recall['fallback_result_count']=None
    except Exception as e: recall.update({'error':type(e).__name__+': '+str(e)[:300]})
(outdir/'recall.json').write_text(json.dumps(recall,indent=2)+'\n')
print(json.dumps({'final_counts':counts,'recall_attempted':recall['attempted'],'recall_http':recall.get('http_status'),'recall_results':recall.get('result_count')}))
