#!/usr/bin/env python3
import hashlib,json,os
from pathlib import Path
ROOT=Path('/tmp/full-docs-backfill')
def h(p):
 x=hashlib.sha256();x.update(p.read_bytes());return x.hexdigest()
for corpus in ['opencode-core','sistersage']:
 root=ROOT/corpus;m=json.loads((root/'provenance-manifest.json').read_text());rd=sorted((root/'runs').iterdir())[-1];s=json.loads((rd/'liv-submissions.json').read_text());o=json.loads((rd/'liv-operations.json').read_text())
 valid=[x for x in m['sources'] if (x.get('status')=='success' or x.get('selection')=='included') and x.get('file')]
 unavailable=[x for x in m['sources'] if x.get('status')=='unavailable']
 all_collected=(not unavailable and len(valid)==m['index_parity']['expected'])
 cls='compliant' if all_collected and o['final']['all_succeeded'] and o['recall'].get('attempted') else ('in-progress' if not o['final']['all_terminal'] else 'not-fully-compliant')
 audit={'schema':'full-docs-backfill-final-audit-v2','corpus':corpus,'classification':cls,'selection_policy':m['selection_policy'],'coverage_claim':m['coverage_claim'],'enumeration':m['index_parity'],'collection':{'valid_collected':len(valid),'unavailable':len(unavailable)},'retention':{'bank':'Liv','selected':s['selected_count'],'accepted_operation_ids':len(s['accepted_operation_ids']),'operations_final':o['final'],'bounded_poll_rounds':len(o['poll_rounds']),'recall':o['recall']},'provenance_manifest_sha256':h(root/'provenance-manifest.json'),'artifacts':{'provenance_manifest':'provenance-manifest.json','coverage':'COVERAGE.md','submission':str((rd/'liv-submissions.json').relative_to(root)),'operations':str((rd/'liv-operations.json').relative_to(root))}}
 (root/'final-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
 ledger=[]
 for p in sorted(root.rglob('*')):
  if p.is_file():ledger.append({'path':str(p.relative_to(root)),'bytes':p.stat().st_size,'sha256':h(p)})
 (root/'SHA256SUMS.json').write_text(json.dumps({'schema':'full-docs-backfill-sha256-v1','corpus':corpus,'files':ledger},indent=2)+'\n')
 (root/'final-audit.md').write_text(f'# {corpus} final audit\n\nClassification: **{cls}**\n\n{m["coverage_claim"]}\n\nLiv: {len(s["accepted_operation_ids"])} accepted operations; final states `{o["final"]["counts"]}`. Recall: `{o["recall"]}`.\n\nProvenance SHA-256: `{audit["provenance_manifest_sha256"]}`.\n')
 print(corpus,cls,len(valid),len(unavailable),len(s['accepted_operation_ids']),o['final']['counts'])
# immutable after all manifests/audits are complete
for root in [ROOT/'opencode-core',ROOT/'sistersage']:
 for p in root.rglob('*'):
  if p.is_file(): p.chmod(0o444)
  elif p.is_dir(): p.chmod(0o555)
 root.chmod(0o555)
