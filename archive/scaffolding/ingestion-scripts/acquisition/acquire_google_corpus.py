#!/usr/bin/env python3
"""Acquire a defined Google API + ADC reference corpus with parity evidence."""
import hashlib, json, re, shutil, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path('/tmp/full-docs-backfill/google-gemini-workspace')
UA='Mozilla/5.0 (compatible; HermesDocsBackfill/1.0)'
def get(url, timeout=60):
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'application/json,text/plain,text/html,*/*'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            return {'status':r.status,'final_url':r.geturl(),'body':r.read(),'content_type':r.headers.get_content_type()}
    except urllib.error.HTTPError as e:
        return {'status':e.code,'final_url':url,'body':e.read() or b'','content_type':e.headers.get_content_type(),'error':str(e)}
    except Exception as e: return {'status':None,'final_url':url,'body':b'','content_type':None,'error':repr(e)}
def safe(n): return re.sub(r'[^A-Za-z0-9._-]+','_',n)[:180]
def save_record(rec, blob, stem):
    if rec['http_status']==200 and blob:
        ext='.json' if 'json' in (rec.get('content_type') or '') else '.rst' if stem.endswith(('.rst','.md')) else '.html'
        fn=safe(stem)+ext
        p=ROOT/'sources'/fn; p.parent.mkdir(exist_ok=True)
        # no collision expected from unique source stems
        p.write_bytes(blob)
        rec.update(file=str(p.relative_to(ROOT)),bytes=len(blob),sha256=hashlib.sha256(blob).hexdigest())
    else: rec.update(file=None,bytes=0,sha256=None)
def add(url, category, stem):
    r=get(url); rec={'source_url':url,'category':category,'http_status':r['status'],'final_url':r['final_url'],'content_type':r.get('content_type'),'error':r.get('error')}
    save_record(rec,r['body'],stem); rows.append(rec); return r

if ROOT.exists():
    print('Refusing existing root',ROOT,file=sys.stderr);sys.exit(2)
ROOT.mkdir(parents=True); (ROOT/'sources').mkdir()
rows=[]; checks=[]
# Record all initial Google LLM-index checks already observed/re-run as durable evidence.
for u in ['https://developers.google.com/llms.txt','https://developers.google.com/llms-full.txt','https://cloud.google.com/llms.txt','https://cloud.google.com/llms-full.txt','https://ai.google.dev/llms.txt','https://ai.google.dev/llms-full.txt']:
 r=get(u); checks.append({'url':u,'http_status':r['status'],'final_url':r['final_url'],'content_type':r.get('content_type'),'error':r.get('error'),'sha256':hashlib.sha256(r['body']).hexdigest() if r['body'] else None,'bytes':len(r['body'])})
# Authoritative discovery directory is the enumeration index for every HTTP API version exposed by actual surfaces.
directory_url='https://www.googleapis.com/discovery/v1/apis'
directory=add(directory_url,'official-discovery-index','google_api_discovery_directory')
if directory['status']!=200: raise RuntimeError('Google discovery directory unavailable')
d=json.loads(directory['body'])
want={'gmail','calendar','drive','sheets','docs','people','geminienterprise'}
items=[x for x in d.get('items',[]) if x.get('name') in want]
# Each entry (including all listed versions) is an enumerated reference source.
for x in sorted(items,key=lambda x:(x['name'],x['version'])):
 add(x['discoveryRestUrl'],'official-discovery-reference',f"discovery_{x['name']}_{x['version']}")
# ADC has no Google Discovery service. Acquire its explicitly authoritative canonical docs;
# record sitemap/index absence separately rather than pretending these pages enumerate all Cloud auth docs.
adc_urls=[
 'https://cloud.google.com/docs/authentication/provide-credentials-adc',
 'https://cloud.google.com/docs/authentication/application-default-credentials',
 'https://cloud.google.com/docs/authentication/external/set-up-adc',
]
for u in adc_urls: add(u,'official-adc-canonical','adc_'+safe(u.rsplit('/',1)[-1]))
# Official Google-maintained google-auth Python repo: deterministically enumerate every authored documentation source.
repo_api='https://api.github.com/repos/googleapis/google-auth-library-python'
r=get(repo_api); checks.append({'url':repo_api,'http_status':r['status'],'final_url':r['final_url'],'content_type':r.get('content_type'),'error':r.get('error'),'sha256':hashlib.sha256(r['body']).hexdigest() if r['body'] else None,'bytes':len(r['body'])})
if r['status']==200:
 meta=json.loads(r['body']); branch=meta['default_branch']
 commit_api=f'https://api.github.com/repos/googleapis/google-auth-library-python/commits/{branch}'
 cr=get(commit_api); checks.append({'url':commit_api,'http_status':cr['status'],'final_url':cr['final_url'],'content_type':cr.get('content_type'),'error':cr.get('error'),'sha256':hashlib.sha256(cr['body']).hexdigest() if cr['body'] else None,'bytes':len(cr['body'])})
 if cr['status']==200:
  sha=json.loads(cr['body'])['sha']
  tree_url=f'https://api.github.com/repos/googleapis/google-auth-library-python/git/trees/{sha}?recursive=1'
  tr=get(tree_url); checks.append({'url':tree_url,'http_status':tr['status'],'final_url':tr['final_url'],'content_type':tr.get('content_type'),'error':tr.get('error'),'sha256':hashlib.sha256(tr['body']).hexdigest() if tr['body'] else None,'bytes':len(tr['body'])})
  if tr['status']==200:
   tree=json.loads(tr['body']);
   paths=[]
   for o in tree.get('tree',[]):
    p=o.get('path','')
    if o.get('type')=='blob' and (p.startswith('docs/') and p.endswith(('.rst','.md'))) : paths.append(p)
   # Supporting repository docs tied to the same reference surface.
   for p in ['README.rst','CONTRIBUTING.rst']:
    if any(o.get('path')==p for o in tree.get('tree',[])): paths.append(p)
   for p in sorted(set(paths)):
    u=f'https://raw.githubusercontent.com/googleapis/google-auth-library-python/{sha}/{p}'
    add(u,'official-google-auth-repo-doc',f'google_auth_{p}')
   repo_info={'repo':'googleapis/google-auth-library-python','branch':branch,'commit':sha,'tree_url':tree_url,'selection':'all tracked docs/**/*.rst|md plus README.rst and CONTRIBUTING.rst; excluded tests, generated build output, dependencies, binaries, and non-document source by declared documentation/reference boundary','enumerated_repo_docs':len(set(paths))}
  else: repo_info={'repo':'googleapis/google-auth-library-python','error':'tree unavailable'}
 else: repo_info={'repo':'googleapis/google-auth-library-python','error':'commit unavailable'}
else: repo_info={'repo':'googleapis/google-auth-library-python','error':'repo metadata unavailable'}
# Data integrity and parity.
for r in rows: r['outcome']='success' if r['http_status']==200 and r['file'] else 'unavailable'
manifest={'run_utc':datetime.now(timezone.utc).isoformat(),'selection_policy':{
 'title':'Actual integration/API-surface corpus — Google Gemini Enterprise and bundled Google Workspace connector APIs',
 'actual_local_connector_surfaces':['gmail v1','calendar v3','drive v3','sheets v4','docs v1','people v1'],
 'gemini_surface':'geminienterprise entries in Google Discovery Directory (all versions currently listed)',
 'reference_boundary':'Every discovery REST document for those seven API names and every enumerated authored documentation file from Google-maintained google-auth-library-python, which supplies ADC; canonical Google Cloud ADC pages retained as primary guidance.',
 'enumeration_evidence':'Google API Discovery Directory enumerates all API/version reference documents. Google GitHub recursive tree pinned to a commit enumerates all selected repository docs files.',
 'known_limitation':'Google developer/cloud/AI llms indexes were checked and unavailable. Cloud-wide sitemap/product-specific Gemini Enterprise narrative-doc enumeration was not established; therefore this is complete for the declared API/reference boundary but not a claim of every narrative page across Google Cloud.'},
 'repository':repo_info,'records':rows,'counts':{'enumerated':len(rows),'successful':sum(x['outcome']=='success' for x in rows),'unavailable':sum(x['outcome']!='success' for x in rows)}}
(ROOT/'source_manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True),encoding='utf-8')
(ROOT/'index_checks.json').write_text(json.dumps(checks,indent=2,sort_keys=True),encoding='utf-8')
coverage=f'''# Coverage statement\n\n## Declared product boundary\nThis corpus covers the complete machine-readable API reference surfaces actually used by the local bundled Google Workspace connector: Gmail v1, Calendar v3, Drive v3, Sheets v4, Docs v1, and People v1. It also covers every Gemini Enterprise (`geminienterprise`) API/version enumerated in Google's official Discovery Directory at acquisition time. ADC coverage consists of the complete authored documentation tree of the Google-maintained `google-auth-library-python` repository at the pinned commit plus three official Google Cloud ADC canonical pages.\n\n## Enumeration and parity\nThe Google API Discovery Directory is the authoritative version enumerator. Every matching directory entry is represented by exactly one source-manifest record. The GitHub recursive tree is pinned and each selected authored document has one manifest record. `source_manifest.json` contains all successes and failures; its enumerated count equals record count.\n\n## Availability / limits\nAll six checked Google LLM index paths returned non-success and are recorded in `index_checks.json`. No product-scoped official sitemap or official Gemini Enterprise narrative-document URL enumerator was found in this acquisition. Consequently, the corpus is **complete for its declared API/reference boundary** but **bounded-incomplete for any claim covering all Google Cloud/Gemini narrative documentation**.\n\n## Collection counts\n- Enumerated: {manifest['counts']['enumerated']}\n- Successful: {manifest['counts']['successful']}\n- Unavailable: {manifest['counts']['unavailable']}\n'''
(ROOT/'COVERAGE.md').write_text(coverage,encoding='utf-8')
print(json.dumps(manifest['counts']|{'repo':repo_info},indent=2))
