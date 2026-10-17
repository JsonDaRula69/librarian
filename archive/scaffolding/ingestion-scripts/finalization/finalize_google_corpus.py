#!/usr/bin/env python3
import concurrent.futures, hashlib,json,re,urllib.request,urllib.error
from pathlib import Path
from datetime import datetime,timezone
R=Path('/tmp/full-docs-backfill/google-gemini-workspace'); S=R/'sources'; S.mkdir(exist_ok=True)
UA={'User-Agent':'HermesDocsBackfill/1.0','Accept':'application/json,text/plain,text/html,*/*'}
def fetch(url):
 try:
  r=urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=35); b=r.read(); return r.status,r.geturl(),r.headers.get_content_type(),b,None
 except urllib.error.HTTPError as e:return e.code,url,e.headers.get_content_type(),e.read() or b'',str(e)
 except Exception as e:return None,url,None,b'',repr(e)
def stem(url):return re.sub('[^A-Za-z0-9._-]+','_',url.replace('https://','').replace('http://',''))[:200]
def store(x):
 url,cat=x; st,final,ct,b,err=fetch(url); rec={'source_url':url,'category':cat,'http_status':st,'final_url':final,'content_type':ct,'error':err}
 if st==200 and b:
  ext='.json' if 'json' in (ct or '') else '.rst' if url.endswith(('.rst','.md')) else '.html'
  p=S/(stem(url)+ext);p.write_bytes(b);rec.update(file=str(p.relative_to(R)),bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),outcome='success')
 else:rec.update(file=None,bytes=0,sha256=None,outcome='unavailable')
 return rec
# dirs and Gemini enumerate
idx='https://www.googleapis.com/discovery/v1/apis'; st,fin,ct,b,er=fetch(idx); directory=json.loads(b)
items=[i for i in directory['items'] if i['name'] in {'gmail','calendar','drive','sheets','docs','people','geminienterprise'}]
print('discovery matches',[(i['name'],i['version']) for i in items],flush=True)
tasks=[(idx,'official-discovery-index')]+[(i['discoveryRestUrl'],'official-discovery-reference') for i in items]
# canonical ADC pages
for u in ['https://cloud.google.com/docs/authentication/provide-credentials-adc','https://cloud.google.com/docs/authentication/application-default-credentials','https://cloud.google.com/docs/authentication/external/set-up-adc']:tasks.append((u,'official-adc-canonical'))
# Official repository docs enumerated by pinned tree
meta=json.loads(fetch('https://api.github.com/repos/googleapis/google-auth-library-python')[3]); branch=meta['default_branch']; commit=json.loads(fetch('https://api.github.com/repos/googleapis/google-auth-library-python/commits/'+branch)[3])['sha']; treeurl='https://api.github.com/repos/googleapis/google-auth-library-python/git/trees/'+commit+'?recursive=1'; tree=json.loads(fetch(treeurl)[3])
paths=sorted(o['path'] for o in tree['tree'] if o.get('type')=='blob' and o['path'].startswith('docs/') and o['path'].endswith(('.rst','.md')))
for p in ['README.rst','CONTRIBUTING.rst']:
 if any(o.get('path')==p for o in tree['tree']):paths.append(p)
for p in paths:tasks.append((f'https://raw.githubusercontent.com/googleapis/google-auth-library-python/{commit}/{p}','official-google-auth-repo-doc'))
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex: rows=list(ex.map(store,tasks))
checks=[]
for u in ['https://developers.google.com/llms.txt','https://developers.google.com/llms-full.txt','https://cloud.google.com/llms.txt','https://cloud.google.com/llms-full.txt','https://ai.google.dev/llms.txt','https://ai.google.dev/llms-full.txt','https://cloud.google.com/sitemap.xml','https://cloud.google.com/gemini/enterprise/sitemap.xml']:
 st,final,ct,b,err=fetch(u); checks.append({'url':u,'http_status':st,'final_url':final,'content_type':ct,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest() if b else None,'error':err})
manifest={'run_utc':datetime.now(timezone.utc).isoformat(),'selection_policy':{'title':'Actual Google integration API/reference corpus','actual_local_connector_surfaces':['gmail v1','calendar v3','drive v3','sheets v4','docs v1','people v1'],'gemini_surface':'all Gemini Enterprise (geminienterprise) entries listed in official Google Discovery Directory','adc_surface':'official canonical Google Cloud ADC pages plus every authored docs/**/*.rst|md and root README.rst/CONTRIBUTING.rst in Google-maintained google-auth-library-python','enumeration_evidence':'Google Discovery Directory for APIs and GitHub recursive tree pinned to a resolved commit for google-auth-library-python docs','limitation':'No valid Google LLM index was available. Cloud-wide/product-specific sitemap checks are recorded; no authoritative Gemini Enterprise narrative-doc URL enumerator was established. Complete only for declared API/reference boundary, not all Google narrative docs.'},'repository':{'repo':'googleapis/google-auth-library-python','branch':branch,'commit':commit,'tree_url':treeurl,'selected_paths':len(paths),'selection':'every docs/**/*.rst|md plus root README.rst and CONTRIBUTING.rst; excluded code, tests, generated output, dependencies, binaries and secrets because boundary is documentation/reference source'},'records':rows,'counts':{'enumerated':len(rows),'successful':sum(x['outcome']=='success' for x in rows),'unavailable':sum(x['outcome']!='success' for x in rows)}}
(R/'source_manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True))
(R/'index_checks.json').write_text(json.dumps(checks,indent=2,sort_keys=True))
(R/'COVERAGE.md').write_text(f'''# Coverage statement\n\n**Declared boundary.** Complete API/reference documentation for the exact bundled Workspace connector APIs (Gmail v1, Calendar v3, Drive v3, Sheets v4, Docs v1, People v1); every current Gemini Enterprise (`geminienterprise`) discovery entry; and ADC's official Cloud canonical pages plus every authored documentation file in the pinned official Python google-auth repository.\n\n**Enumeration parity.** Google Discovery Directory produced every selected API/version source; GitHub's recursive tree at `{commit}` produced every selected repository-document source. `source_manifest.json` has exactly one record per enumerated source, whether successful or unavailable.\n\n**Availability/limits.** The checked llms.txt/llms-full.txt and sitemap paths with statuses are in `index_checks.json`. No product-specific official Gemini Enterprise narrative-doc enumerator was established. Thus this is **complete for its declared API/reference boundary** and **bounded-incomplete** for a broader claim over all Google Cloud/Gemini narrative documentation.\n\n**Counts.** Enumerated {manifest['counts']['enumerated']}; successful {manifest['counts']['successful']}; unavailable {manifest['counts']['unavailable']}.\n''')
print(json.dumps(manifest['counts']|{'commit':commit,'repository_docs':len(paths)},indent=2))
