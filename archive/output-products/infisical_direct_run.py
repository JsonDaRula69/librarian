import hashlib, json, os, re, time
from pathlib import Path
from urllib.parse import urlparse
import urllib.request, urllib.error
import requests

ROOT=Path('/home/jsondarula/Liv-Home/Doc Corpus')
RUN=ROOT/'reports/runs/infisical-direct-20260827'
SRC=ROOT/'sources/first-party/infisical'
INDEX_URL='https://infisical.com/docs/llms.txt'
BASE='http://127.0.0.1:8888'; BANK='Liv'
RUN.mkdir(parents=True, exist_ok=True)
SRC.mkdir(parents=True, exist_ok=True)
(SRC/'sources').mkdir(exist_ok=True)

# Fetch bytes and retain transport evidence without exposing response bodies in reports.
def fetch(url):
    req=urllib.request.Request(url,headers={'User-Agent':'Liv-Infisical-Direct/1.0'})
    try:
        with urllib.request.urlopen(req,timeout=45) as r:
            return {'http_status':r.status,'final_url':r.geturl(),'content_type':r.headers.get('Content-Type',''),'bytes':r.read(),'error':None}
    except urllib.error.HTTPError as e:
        b=e.read()
        return {'http_status':e.code,'final_url':e.geturl(),'content_type':e.headers.get('Content-Type',''),'bytes':b,'error':None}
    except Exception as e:
        return {'http_status':None,'final_url':url,'content_type':'','bytes':b'','error':type(e).__name__+': '+str(e)[:300]}

def sha(b): return hashlib.sha256(b).hexdigest() if b else None

def safe_name(i,url):
    p=urlparse(url); base=Path(p.path).name or 'index.md'
    # Preserve original suffix; hash avoids collisions while retaining readable basename.
    return f'{i:04d}_{hashlib.sha256(url.encode()).hexdigest()[:12]}_{base}'

idx=fetch(INDEX_URL); (RUN/'llms.txt').write_bytes(idx['bytes'])
text=idx['bytes'].decode('utf-8','replace')
links=re.findall(r'\[[^\]]*\]\((https?://[^)]+)\)',text)
# One manifest row per distinct listed URL; retain exact occurrence accounting separately.
urls=[]
for u in links:
    if u not in urls: urls.append(u)
mdlinks=[u for u in urls if u.lower().endswith('.md')]
validation={'schema':'infisical-index-validation-v1','index_url':INDEX_URL,'index_http_status':idx['http_status'],'index_final_url':idx['final_url'],'index_content_type':idx['content_type'],'index_bytes':len(idx['bytes']),'index_sha256':sha(idx['bytes']),'markdown_link_occurrences':sum(u.lower().endswith('.md') for u in links),'all_link_occurrences':len(links),'unique_markdown_urls':len(mdlinks),'unique_all_urls':len(urls),'duplicate_urls':sorted({u for u in links if links.count(u)>1}),'extraction':'Markdown inline links; distinct URL rows preserve first listed occurrence; no URL normalization.'}
(RUN/'index-validation.json').write_text(json.dumps(validation,indent=2,sort_keys=True)+'\n')

rows=[]
for i,u in enumerate(urls,1):
    r=fetch(u); recovery=None; canonical=None
    if r['http_status']==404 and u.lower().endswith('.md'):
        canonical=u[:-3]
        recovery=fetch(canonical)
    chosen=recovery if recovery and recovery['http_status']==200 else r
    success=chosen['http_status']==200 and bool(chosen['bytes'])
    local=None; nbytes=0; digest=None
    if success:
        local=Path('sources/first-party/infisical/sources')/safe_name(i,u)
        (SRC/'sources'/local.name).write_bytes(chosen['bytes'])
        nbytes=len(chosen['bytes']); digest=sha(chosen['bytes'])
    row={'listed_url':u,'final_url':chosen['final_url'],'http_status':chosen['http_status'],'status':'success' if success else 'unavailable','local_path':str(local) if local else None,'byte_count':nbytes,'sha256':digest,'provenance':{'index_url':INDEX_URL,'listed_occurrence':links.index(u)+1,'recovery_attempted':bool(recovery),'initial_http_status':r['http_status'],'initial_final_url':r['final_url'],'initial_error':r['error'],'recovery_url':canonical if recovery else None,'recovery_http_status':recovery['http_status'] if recovery else None,'recovery_final_url':recovery['final_url'] if recovery else None,'recovery_error':recovery['error'] if recovery else None}}
    rows.append(row)
manifest={'schema':'infisical-direct-source-manifest-v1','index_url':INDEX_URL,'enumeration':'every distinct inline Markdown/URL link in the byte snapshot; duplicate link occurrences are recorded in validation','records':rows,'counts':{'listed_unique_urls':len(rows),'listed_occurrences':len(links),'successful':sum(x['status']=='success' for x in rows),'unavailable':sum(x['status']!='success' for x in rows)}}
(ROOT/'manifests').mkdir(exist_ok=True)
(ROOT/'manifests/infisical-direct-manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')

# Prepare .md copies and a durable Liv+hash ledger. Ledger is append-only JSONL.
prep=RUN/'prepared'; prep.mkdir()
ledger=ROOT/'dedupe/infisical-liv-content-ledger.jsonl'; ledger.parent.mkdir(exist_ok=True)
existing={}
if ledger.exists():
    for line in ledger.read_text().splitlines():
        try:
            x=json.loads(line); existing.setdefault(x.get('scope_hash'),[]).append(x)
        except Exception: pass
items=[]; skipped=[]
for row in rows:
    if row['status']!='success': continue
    source=ROOT/row['local_path']
    b=source.read_bytes(); assert sha(b)==row['sha256'] and b
    name=Path(row['local_path']).stem+'.md'
    # If source already .md, stem preserves suffix exactly once.
    pp=prep/name
    pp.write_bytes(b)
    pb=pp.read_bytes(); assert sha(pb)==sha(b) and pp.suffix=='.md'
    scope_hash=f'Liv:{sha(pb)}'
    prior=existing.get(scope_hash,[])
    active=any(x.get('state') in ('claimed','submitted','active','completed','success') for x in prior)
    item={'listed_url':row['listed_url'],'source_path':row['local_path'],'prepared_path':str(pp.relative_to(ROOT)),'sha256':sha(pb),'byte_count':len(pb),'scope_hash':scope_hash,'context':f"Official Infisical documentation from {row['listed_url']} (enumerated by {INDEX_URL})",'tags':['official-documentation','infisical','infisical-docs','liv']}
    if active:
        skipped.append({**item,'reason':'hash already active or successfully submitted; provenance alias recorded'})
    else: items.append(item)
prep_manifest={'schema':'infisical-prepared-manifest-v1','items':items,'skipped_duplicate_hashes':skipped,'counts':{'successful_sources':manifest['counts']['successful'],'prepared_new':len(items),'skipped_duplicates':len(skipped)}}
(RUN/'prepared-manifest.json').write_text(json.dumps(prep_manifest,indent=2,sort_keys=True)+'\n')

# Read only approved credential line; never serialize it.
env=Path('/home/jsondarula/.hermes/.env').read_text()
m=re.search(r'^HINDSIGHT_API_KEY\s*=\s*(.+?)\s*$',env,re.M)
if not m: raise RuntimeError('authorized HINDSIGHT_API_KEY unavailable')
key=m.group(1).strip().strip("'\"")
s=requests.Session(); headers={'Authorization':'Bearer '+key}
pre=s.get(BASE+'/v1/default/banks',headers=headers,timeout=30)
try: pb=pre.json()
except Exception: pb={}
banks=pb.get('banks',pb.get('data',[])) if isinstance(pb,dict) else []
found=any(isinstance(x,dict) and (x.get('name')==BANK or x.get('bank_id')==BANK or x.get('id')==BANK) for x in banks)
(RUN/'preflight.json').write_text(json.dumps({'endpoint':BASE+'/v1/default/banks','http_status':pre.status_code,'bank':'Liv','bank_found':found,'response_keys':sorted(pb.keys()) if isinstance(pb,dict) else []},indent=2)+'\n')
if pre.status_code!=200 or not found: raise RuntimeError(f'Liv preflight failed HTTP {pre.status_code} found={found}')

sub={'schema':'infisical-liv-submissions-v1','bank':'Liv','preflight_http_status':pre.status_code,'batch_size':3,'selected_new':len(items),'skipped_duplicates':len(skipped),'batches':[],'accepted_operation_ids':[]}
(RUN/'submissions.json').write_text(json.dumps(sub,indent=2)+'\n')
for start in range(0,len(items),3):
    batch=items[start:start+3]; files=[]; handles=[]
    try:
        metas=[]
        for it in batch:
            p=ROOT/it['prepared_path']; b=p.read_bytes(); assert p.suffix=='.md' and b and sha(b)==it['sha256']
            f=open(p,'rb'); handles.append(f); files.append(('files',(p.name,f,'text/markdown; charset=utf-8')))
            metas.append({'context':it['context'],'tags':it['tags']})
        resp=s.post(BASE+'/v1/default/banks/Liv/files/retain',headers=headers,files=files,data={'request':json.dumps({'files_metadata':metas})},timeout=180)
        try: body=resp.json()
        except Exception: body={}
        ids=body.get('operation_ids',[]) if isinstance(body,dict) else []
        accepted=resp.status_code==200 and len(ids)==len(batch)
        safe={'batch_number':len(sub['batches'])+1,'file_hashes':[it['sha256'] for it in batch],'prepared_paths':[it['prepared_path'] for it in batch],'listed_urls':[it['listed_url'] for it in batch],'http_status':resp.status_code,'accepted':accepted,'operation_ids':ids,'operation_count':len(ids)}
    finally:
        for f in handles: f.close()
    sub['batches'].append(safe); (RUN/'submissions.json').write_text(json.dumps(sub,indent=2)+'\n')
    if not accepted: raise RuntimeError(f'upload rejected batch {safe["batch_number"]} HTTP {safe["http_status"]} ids={len(ids)} expected={len(batch)}')
    sub['accepted_operation_ids'].extend(ids); (RUN/'submissions.json').write_text(json.dumps(sub,indent=2)+'\n')
    # Claim/submit each exact prepared hash only after accepted response is durably recorded.
    for it,oid in zip(batch,ids):
        rec={**it,'state':'submitted','operation_id':oid,'recorded_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'run':'infisical-direct-20260827'}
        with ledger.open('a') as f: f.write(json.dumps(rec,sort_keys=True)+'\n')

# Bounded polling: two immediate rounds, no monitoring loop.
ops=sub['accepted_operation_ids']; rounds=[]; terminal={'completed','failed','cancelled','not_found'}
for rnd in range(1,3):
    statuses=[]
    for oid in ops:
        r=s.get(f'{BASE}/v1/default/banks/Liv/operations/{oid}',headers=headers,timeout=30)
        try: b=r.json()
        except Exception: b={}
        statuses.append({'operation_id':oid,'poll_round':rnd,'http_status':r.status_code,'state':b.get('status') if isinstance(b,dict) else None})
    rounds.append(statuses)
    if all(x['state'] in terminal for x in statuses): break
ops_report={'schema':'infisical-liv-operations-v1','operation_count':len(ops),'poll_rounds':rounds,'final':rounds[-1] if rounds else [],'counts':{}}
for x in ops_report['final']: ops_report['counts'][x['state'] or 'unknown']=ops_report['counts'].get(x['state'] or 'unknown',0)+1
(RUN/'operations.json').write_text(json.dumps(ops_report,indent=2,sort_keys=True)+'\n')
all_terminal=bool(ops) and all(x['state'] in terminal for x in ops_report['final'])
all_success=all_terminal and all(x['state']=='completed' and x['http_status']==200 for x in ops_report['final'])
recall={'status':'pending/skipped','reason':'not all required operations terminal and successful' if not all_success else 'not run'}
if all_success:
    rr=s.post(BASE+'/v1/default/banks/Liv/memories/recall',headers={**headers,'Content-Type':'application/json'},json={'query':'Infisical machine identities and universal authentication','filters':{'tags':['infisical-docs']}},timeout=60)
    try: rb=rr.json()
    except Exception: rb={}
    recall={'status':'completed','http_status':rr.status_code,'result_count':len(rb.get('memories',rb.get('results',[]))) if isinstance(rb,dict) else None}
(RUN/'recall.json').write_text(json.dumps(recall,indent=2,sort_keys=True)+'\n')

final={'schema':'infisical-direct-final-audit-v1','classification':'compliant' if manifest['counts']['unavailable']==0 and all_success else ('in-progress' if not all_terminal else 'bounded-incomplete'),'counts':{'listed_occurrences':len(links),'unique_listed_urls':len(urls),'successful_sources':manifest['counts']['successful'],'unavailable_sources':manifest['counts']['unavailable'],'prepared_new':len(items),'skipped_duplicate_hashes':len(skipped),'submitted_files':len(items),'accepted_operation_ids':len(ops),'operations_with_poll_results':len(ops_report['final'])},'parity':{'manifest_rows_equal_unique_urls':len(rows)==len(urls),'submitted_files_equal_operation_ids':len(items)==len(ops),'all_operations_polled':len(ops)==len(ops_report['final'])},'poll_counts':ops_report['counts'],'recall':recall,'artifacts':{'index_snapshot':str(RUN/'llms.txt'),'index_validation':str(RUN/'index-validation.json'),'manifest':str(ROOT/'manifests/infisical-direct-manifest.json'),'prepared_manifest':str(RUN/'prepared-manifest.json'),'preflight':str(RUN/'preflight.json'),'submissions':str(RUN/'submissions.json'),'operations':str(RUN/'operations.json'),'recall':str(RUN/'recall.json'),'ledger':str(ledger),'sources':str(SRC),'prepared':str(prep)}}
(RUN/'final-audit.json').write_text(json.dumps(final,indent=2,sort_keys=True)+'\n')
(RUN/'FINAL.md').write_text('# Infisical direct ingestion\n\nClassification: **'+final['classification']+'**\n\n- Unique listed URLs: '+str(len(urls))+'; successful: '+str(manifest['counts']['successful'])+'; unavailable: '+str(manifest['counts']['unavailable'])+'\n- New prepared/submitted: '+str(len(items))+'; duplicate hashes skipped before POST: '+str(len(skipped))+'\n- Accepted operations: '+str(len(ops))+'; polled: '+str(len(ops_report['final']))+'; recall: '+recall['status']+'\n\nSee `final-audit.json` for exact artifact paths and parity checks.\n')
print(json.dumps(final,sort_keys=True))
