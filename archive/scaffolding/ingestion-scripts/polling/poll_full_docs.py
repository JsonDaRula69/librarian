import re,json,time
from pathlib import Path
import requests
ROOT=Path('/tmp/full-docs-backfill'); BASE='http://127.0.0.1:8888'; BANK='Liv'; TERMINAL={'completed','failed','cancelled','not_found'}
def key():
 s=Path('/home/jsondarula/.hermes/.env').read_text(); return re.search(r'^HINDSIGHT_API_KEY\s*=\s*(.+?)\s*$',s,re.M).group(1).strip().strip("'\"")
def main():
 h={'Authorization':'Bearer '+key()}
 for product in ('hindsight','ollama'):
  rdir=sorted((ROOT/product/'runs').iterdir())[-1]; subs=json.loads((rdir/'liv-submissions.json').read_text()); ids=subs['accepted_operation_ids']
  out={'schema':'liv-operations-v1','run_id':subs['run_id'],'product':product,'bank':BANK,'operation_count':len(ids),'poll_rounds':[],'final':{}}
  latest={}
  for rnd in range(1,5):
   states=[]
   for oid in ids:
    resp=requests.get(f'{BASE}/v1/default/banks/{BANK}/operations/{oid}',headers=h,timeout=30)
    try: d=resp.json()
    except Exception: d={}
    st=d.get('status') if resp.status_code==200 else ('not_found' if resp.status_code==404 else 'http_error')
    latest[oid]={'operation_id':oid,'http_status':resp.status_code,'state':st}
    states.append(st)
   counts={x:states.count(x) for x in sorted(set(states))}; out['poll_rounds'].append({'round':rnd,'at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'counts':counts,'operations':[latest[x] for x in ids]})
   (rdir/'liv-operations.json').write_text(json.dumps(out,indent=2)+'\n')
   if all(x in TERMINAL for x in states): break
   time.sleep(5)
  fc={}
  for x in latest.values(): fc[x['state']]=fc.get(x['state'],0)+1
  out['final']={'counts':fc,'all_terminal':all(x['state'] in TERMINAL for x in latest.values()),'all_succeeded':len(latest)==len(ids) and all(x['state']=='completed' for x in latest.values())}
  (rdir/'liv-operations.json').write_text(json.dumps(out,indent=2)+'\n')
  print(product,subs['run_id'],out['final'])
if __name__=='__main__':main()
