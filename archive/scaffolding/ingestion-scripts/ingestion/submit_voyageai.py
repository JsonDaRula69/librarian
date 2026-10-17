#!/usr/bin/env python3
"""Submit all downloaded VoyageAI docs to Liv, then poll every returned operation."""
import json, sys, time, uuid
from pathlib import Path
import requests
sys.path.insert(0, '/home/jsondarula')
from voyageai_ingest import API, MANIFEST, headers

ROOT=Path('/tmp/voyageai_docs')
OPS=ROOT/'operations.json'
BATCH=5
BANK='Liv'
manifest=json.loads(MANIFEST.read_text())
files=[r for r in manifest['records'] if r['download_status'] in ('downloaded','canonical_recovered')]
s=requests.Session(); s.headers.update(headers())
ops=[]; submissions=[]
base_context='Official Voyage AI documentation, sourced from https://docs.voyageai.com/llms.txt'
for start in range(0,len(files),BATCH):
    batch=files[start:start+BATCH]
    # The API requires source context/tags to be paired with their corresponding file.
    request_data={'files_metadata':[{'context':base_context+' Source: '+rec['source_url'], 'tags':['documentation','voyageai','official']} for rec in batch]}
    handles=[]
    try:
        for rec in batch:
            f=open(rec['local_path'],'rb'); handles.append(f)
        multipart=[('files',(Path(rec['local_path']).name,h,'text/markdown')) for rec,h in zip(batch,handles)]
        r=s.post(f'{API}/banks/{BANK}/files/retain',files=multipart,data={'request':json.dumps(request_data)},timeout=120)
        out={'batch':start//BATCH+1,'source_files':[x['source_url'] for x in batch],'http_status':r.status_code}
        try: out['response']=r.json()
        except Exception: out['response_text']=r.text[:2000]
        if r.status_code==200: ops.extend(out['response'].get('operation_ids',[]))
        submissions.append(out)
        print('SUBMIT',out['batch'],'http='+str(r.status_code),'ops='+str(len(out.get('response',{}).get('operation_ids',[]))),flush=True)
    finally:
        for h in handles: h.close()
    if r.status_code != 200: break
state={'bank_id':BANK,'request':request_data,'submission_count':len(submissions),'submissions':submissions,'operation_ids':ops,'polls':{}}
OPS.write_text(json.dumps(state,indent=2))
if len(ops)!=len(files):
    print('STOP submission mismatch files='+str(len(files))+' operations='+str(len(ops))); sys.exit(2)
# All docs sent. Poll tracked operations up to 30 minutes; preserve last terminal response.
pending=set(ops); deadline=time.time()+1800; round_=0
while pending and time.time()<deadline:
    round_+=1
    for op in list(pending):
        r=s.get(f'{API}/banks/{BANK}/operations/{op}',timeout=30)
        item={'http_status':r.status_code}
        try: item.update(r.json())
        except Exception: item['response_text']=r.text[:1000]
        state['polls'][op]=item
        if item.get('status') in {'completed','failed','cancelled','not_found'}: pending.remove(op)
    OPS.write_text(json.dumps(state,indent=2))
    counts={}
    for x in state['polls'].values(): counts[x.get('status','unknown')]=counts.get(x.get('status','unknown'),0)+1
    print('POLL',round_,'terminal='+str(len(ops)-len(pending))+'/'+str(len(ops)),'counts='+json.dumps(counts,sort_keys=True),flush=True)
    if pending: time.sleep(min(10, 2+round_))
print('OPERATIONS='+str(OPS))
print('PENDING='+str(len(pending)))
if pending: sys.exit(3)
