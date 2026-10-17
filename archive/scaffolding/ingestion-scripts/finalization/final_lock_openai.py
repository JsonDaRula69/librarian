import hashlib,json,os,time
from pathlib import Path
import requests
r=Path('/tmp/full-docs-backfill/openai-codex'); e=Path('/home/jsondarula/.hermes/.env'); key=next(line.split('=',1)[1].strip().strip('"').strip("'") for line in e.read_text().splitlines() if line.strip().startswith('HINDSIGHT_API_KEY=')); h={'Authorization':'Bearer '+key}
retry=sorted((r/'retention').glob('retry-*/submission.json'))[-1]; sub=json.loads(retry.read_text()); hist=[]
for n in range(1,3):
 q=requests.get('http://127.0.0.1:8888/v1/default/banks/Liv/operations/'+sub['operation_id'],headers=h,timeout=30); b=q.json(); hist.append({'poll':n,'http_status':q.status_code,'status':b.get('status')});
 if b.get('status') in ('completed','failed','cancelled','not_found'): break
 if n==1: time.sleep(2)
(retry.parent/'operation_status.json').write_text(json.dumps({'operation_id':sub['operation_id'],'status_checks':hist},indent=2)+'\n')
# update final audit, preserving declared condition and flagging the first accept whose local response was not persisted
for p in [r/'final_audit.json',r/'immutable_hashes.json']:
 try:os.chmod(p,0o644)
 except:pass
old=json.loads((r/'final_audit.json').read_text()); old['coverage']=json.loads((r/'coverage.json').read_text())['counts']; old['retention']['accepted_operations']=544; old['retention']['retry_source_operation_id']=sub['operation_id']; old['retention']['retry_operation_final_status']=hist[-1]['status']; old['retention']['unrecorded_acceptance_event']='A first retry retain request returned HTTP 200 before its run-directory write failed due to an immutable retention directory; its response operation ID was not persisted. A second separately recorded retain of that same recovered source was accepted as operation '+sub['operation_id']+'.'; (r/'final_audit.json').write_text(json.dumps(old,indent=2)+'\n')
items=[]
for p in sorted(r.rglob('*')):
 if p.is_file() and p.name!='immutable_hashes.json': items.append({'path':str(p.relative_to(r)),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
(r/'immutable_hashes.json').write_text(json.dumps({'policy':'Read-only audit snapshot. Immutable source content is SHA-256 pinned in source_manifest.json; this inventory pins all corpus artifacts generated before lock.','files':items},indent=2)+'\n')
for p in sorted(r.rglob('*'),reverse=True):
 try: os.chmod(p,0o444 if p.is_file() else 0o555)
 except:pass
os.chmod(r,0o555)
print(json.dumps({'retry_status':hist[-1]['status'],'locked':True,'artifact_files':len(items)+1}))
