import json,time
from pathlib import Path
import requests
r=Path('/tmp/full-docs-backfill/openai-codex'); e=Path('/home/jsondarula/.hermes/.env'); key=next(line.split('=',1)[1].strip().strip('"').strip("'") for line in e.read_text().splitlines() if line.strip().startswith('HINDSIGHT_API_KEY=')); h={'Authorization':'Bearer '+key}
it=json.loads((r/'prepared_retention_manifest.json').read_text())[-1]; p=r/it['upload_file'];
pre=requests.get('http://127.0.0.1:8888/v1/default/banks',headers=h,timeout=30); assert pre.status_code==200
with open(p,'rb') as f:
 q=requests.post('http://127.0.0.1:8888/v1/default/banks/Liv/files/retain',headers=h,files=[('files',(p.name,f,'text/markdown; charset=utf-8'))],data={'request':json.dumps({'files_metadata':[{'context':it['context'],'tags':it['tags']}]})},timeout=180)
b=q.json(); assert q.status_code==200 and len(b.get('operation_ids',[]))==1
out={'run_id':'retry-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()),'preflight_http_status':pre.status_code,'source_url':it['source_url'],'filename':p.name,'http_status':q.status_code,'accepted':True,'operation_id':b['operation_ids'][0],'poll_condition':'At most two polls; recall remains skipped because primary full corpus run is pending.'}
d=r/'retention'/out['run_id']; d.mkdir(); (d/'submission.json').write_text(json.dumps(out,indent=2)+'\n'); print(json.dumps(out))
