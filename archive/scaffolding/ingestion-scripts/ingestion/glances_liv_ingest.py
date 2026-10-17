import json, os, re, sys, hashlib, subprocess, shutil
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone

ROOT=Path('/home/jsondarula/Liv-Home/Doc Corpus/sources/open-source/glances')
ROOT.mkdir(exist_ok=True)
MANIFESTS=ROOT/'manifests'
UA='Hermes-Documentation-Ingestion/1.0'
indexes=['https://glances.readthedocs.io/llms.txt','https://glances.readthedocs.io/llms-full.txt','https://nicolargo.github.io/glances/llms.txt','https://nicolargo.github.io/glances/llms-full.txt']
def fetch(url, timeout=30):
    req=Request(url,headers={'User-Agent':UA})
    try:
        with urlopen(req,timeout=timeout) as r:
            b=r.read(); return {'url':url,'final_url':r.url,'http_status':r.status,'content_type':r.headers.get_content_type(),'bytes':len(b),'body':b.decode('utf-8','replace')}
    except HTTPError as e: return {'url':url,'final_url':url,'http_status':e.code,'content_type':e.headers.get_content_type() if e.headers else None,'bytes':0,'error':str(e)}
    except Exception as e: return {'url':url,'final_url':url,'http_status':None,'content_type':None,'bytes':0,'error':repr(e)}
idx=[]
for i,u in enumerate(indexes):
    r=fetch(u); body=r.pop('body','')
    p=ROOT/f'index-check-{i+1}.txt'; p.write_text(body,encoding='utf-8') if body else p.write_text('',encoding='utf-8')
    r['snapshot']=p.name; r['sha256']=hashlib.sha256(body.encode()).hexdigest() if body else None
    r['valid_document_index']=bool(r['http_status']==200 and len(body)>0 and ('#' in body or 'http' in body) and '<html' not in body.lower()[:1000])
    idx.append(r)
(MANIFESTS/'index-checks.json').write_text(json.dumps(idx,indent=2),encoding='utf-8')
valid=[x for x in idx if x['valid_document_index']]
print(json.dumps({'indexes':[(x['url'],x['http_status'],x['valid_document_index']) for x in idx],'valid_count':len(valid)},indent=2))
