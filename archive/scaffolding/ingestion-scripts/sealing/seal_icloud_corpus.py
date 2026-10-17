import json,hashlib,os,time
from pathlib import Path
ROOT=Path('/tmp/full-docs-backfill/icloud-integration')
m=json.loads((ROOT/'manifest.json').read_text()); sub=json.loads((ROOT/'liv-submissions.json').read_text()); op=json.loads((ROOT/'liv-operations.json').read_text()); rec=json.loads((ROOT/'liv-recall.json').read_text())
counts=op['rounds'][-1]['counts']; accepted=sum(len(x['operation_ids']) for x in sub['batches'])
audit={'sealed_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'classification':'in-progress: complete only within declared explicit source boundary; 401 valid artifacts submitted, but all operations pending after three bounded polling rounds and recall correctly skipped','selection_policy':m['boundary'],'enumeration_parity':m['parity'],'ingestion':{'valid_submitted':accepted,'batch_count':len(sub['batches']),'operation_status_counts':counts,'recall':rec},'index_checks':[{k:x.get(k) for k in ['source_url','http_status','valid']} for x in m['index_checks']],'repositories':[{'name':x['name'],'repo':x['repo'],'commit':x['commit'],'selected_count':x['selected_count']} for x in m['repositories']]}
(ROOT/'final-audit.json').write_text(json.dumps(audit,indent=2))
(ROOT/'coverage-statement.md').write_text('# iCloud integration corpus — sealed coverage statement\n\n**Classification:** in-progress. The corpus is complete only within the declared boundary below, not all Apple documentation. All 401 valid artifacts were accepted by Liv Hindsight in 101 batches; after three bounded status-poll rounds, all 401 were still pending, so recall was skipped.\n\n## Boundary\n\n'+m['boundary']['apple']+'\n\n'+m['boundary']['tools']+'\n\n## Enumeration parity\n\n- Enumerated records: 403\n- Valid/downloaded source artifacts: 401\n- Explicit unavailable records: 2\n- Prepared and submitted artifacts: 401\n- Accepted Hindsight operation IDs: 401\n\nSee `manifest.json`, `liv-submissions.json`, `liv-operations.json`, `liv-recall.json`, and `final-audit.json`.\n')
lines=[]
for p in sorted(ROOT.rglob('*')):
 if p.is_file() and '.git' not in p.parts and p.name!='SHA256SUMS': lines.append(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(ROOT)))
(ROOT/'SHA256SUMS').write_text('\n'.join(lines)+'\n')
# immutable after all evidence/ledger writes
for p in sorted(ROOT.rglob('*'),key=lambda x:len(x.parts),reverse=True):
 try: os.chmod(p,0o555 if p.is_dir() else 0o444)
 except PermissionError: pass
os.chmod(ROOT,0o555)
print(json.dumps({'audit':str(ROOT/'final-audit.json'),'ledger_entries':len(lines),'accepted':accepted,'pending':counts.get('pending',0),'sealed':True}))
