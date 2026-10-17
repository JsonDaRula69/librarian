import hashlib,json,os
from pathlib import Path
import requests
R=Path('/tmp/full-docs-backfill/openai-codex'); prior=Path('/tmp/openai_llm_docs')
E=Path('/home/jsondarula/.hermes/.env'); key=None
for line in E.read_text().splitlines():
    if line.strip().startswith('HINDSIGHT_API_KEY='): key=line.split('=',1)[1].strip().strip('"').strip("'"); break
h={'Authorization':'Bearer '+key}; base='http://127.0.0.1:8888'
old_repair=json.loads((prior/'repaired/hindsight_repair_operations.json').read_text())['operations']
old_retry=json.loads((prior/'retry_ingestion.json').read_text())
ops=[{'record':'repaired/hindsight_repair_operations.json','operation_id':x['operation_id'],'source_path':x['source_path']} for x in old_repair]
ops += [{'record':'retry_ingestion.json','operation_id':x,'source_path':p} for x,p in zip(old_retry['operation_ids'],old_retry['files'])]
for x in ops:
    try:
        q=requests.get(base+'/v1/default/banks/Liv/operations/'+x['operation_id'],headers=h,timeout=30); b=q.json() if q.headers.get('Content-Type','').startswith('application/json') else {}; x['current_http_status']=q.status_code;x['current_operation_status']=b.get('status','not_found' if q.status_code==404 else None)
    except Exception as e:x['current_http_status']=None;x['current_operation_status']=None;x['error']=type(e).__name__
recon=json.loads((R/'reconciliation.json').read_text()); recon['prior_repair_and_retry_operation_reconciliation']=ops
recon['repair_reconciliation_summary']={'tracked_operations':len(ops),'completed':sum(x.get('current_operation_status')=='completed' for x in ops),'pending':sum(x.get('current_operation_status')=='pending' for x in ops),'failed':sum(x.get('current_operation_status')=='failed' for x in ops),'cancelled':sum(x.get('current_operation_status')=='cancelled' for x in ops),'not_found':sum(x.get('current_operation_status')=='not_found' for x in ops)}
(R/'reconciliation.json').write_text(json.dumps(recon,indent=2)+'\n')
sub=next((R/'retention').glob('*/submission_summary.json')); st=json.loads((sub.parent/'operation_statuses.json').read_text()); recall=json.loads((sub.parent/'recall.json').read_text()); cov=json.loads((R/'coverage.json').read_text())
report={'classification':'in-progress','reason':'All valid source files were accepted for retention, but bounded polling left every new operation pending; recall correctly skipped.', 'coverage':cov['counts'],'retention':{'run_id':st['run_id'],'accepted_operations':sum(len(b['operation_ids']) for b in json.loads(sub.read_text())['batches']),'final_operation_counts':st['final_counts'],'recall':recall},'prior_repair_reconciliation':recon['repair_reconciliation_summary']}
(R/'final_audit.json').write_text(json.dumps(report,indent=2)+'\n')
# snapshot hashes of every file before lock; hash inventory itself intentionally excluded from its own listing
items=[]
for p in sorted(R.rglob('*')):
    if p.is_file() and p.name!='immutable_hashes.json':
        items.append({'path':str(p.relative_to(R)),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
(R/'immutable_hashes.json').write_text(json.dumps({'policy':'Read-only audit snapshot. Immutable source content is SHA-256 pinned in source_manifest.json; this inventory pins all corpus artifacts generated before lock.','files':items},indent=2)+'\n')
for p in sorted(R.rglob('*'),reverse=True):
    try:
        if p.is_file(): os.chmod(p,0o444)
        elif p.is_dir(): os.chmod(p,0o555)
    except PermissionError: pass
print(json.dumps({'repair_statuses':recon['repair_reconciliation_summary'],'audit_classification':report['classification'],'locked_files':len(items)+1}))
