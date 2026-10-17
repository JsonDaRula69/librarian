import json,os
from pathlib import Path
import requests
E=Path('/home/jsondarula/.hermes/.env')
key=None
for line in E.read_text().splitlines():
    if line.strip().startswith('HINDSIGHT_API_KEY='):
        key=line.split('=',1)[1].strip().strip('"').strip("'")
        break
if not key: raise SystemExit('HINDSIGHT_API_KEY absent')
base='http://127.0.0.1:8888'
h={'Authorization':'Bearer '+key}
r=requests.get(base+'/v1/default/banks',headers=h,timeout=30)
out={'endpoint':base,'http_status':r.status_code,'ok':r.status_code==200,'bank_names':[],'bank_ids':[]}
if r.status_code==200:
    try:
        x=r.json(); banks=x.get('banks',x.get('data',[]))
        for b in banks:
            out['bank_names'].append(b.get('name')); out['bank_ids'].append(b.get('bank_id',b.get('id')))
    except Exception: out['parse_error']='non-json'
print(json.dumps(out))
