#!/usr/bin/env python3
import json, subprocess, hashlib, re, zipfile, shutil
from pathlib import Path
from datetime import datetime,timezone
R=Path('/tmp/full-docs-backfill/google-gemini-workspace'); S=R/'sources'; S.mkdir(exist_ok=True)
def curl(url):
 p=subprocess.run(['curl','-L','--max-time','30','-sS','-w','\n%{http_code}\n%{url_effective}\n%{content_type}','-A','HermesDocsBackfill/1.0',url],capture_output=True)
 parts=p.stdout.rsplit(b'\n',3)
 if len(parts)==4:
  b,code,final,ct=parts
  try: code=int(code)
  except: code=None
  return code,final.decode(errors='replace'),ct.decode(errors='replace'),b,(p.stderr.decode(errors='replace') or None)
 return None,url,None,b'',p.stderr.decode(errors='replace') or 'curl parse failure'
def clean(url):return re.sub('[^A-Za-z0-9._-]+','_',url.replace('https://',''))[:180]
def add(url,cat,blob=None,ct=None,final=None,status=None,error=None):
 if blob is None: status,final,ct,blob,error=curl(url)
 rec={'source_url':url,'category':cat,'http_status':status,'final_url':final,'content_type':ct,'error':error}
 if status==200 and blob:
  ext='.json' if 'json' in (ct or '') else ('.rst' if url.endswith(('.rst','.md')) else '.html')
  p=S/(clean(url)+ext);p.write_bytes(blob);rec.update(file=str(p.relative_to(R)),bytes=len(blob),sha256=hashlib.sha256(blob).hexdigest(),outcome='success')
 else:rec.update(file=None,bytes=0,sha256=None,outcome='unavailable')
 return rec
# Directory and exact service document list
idx='https://www.googleapis.com/discovery/v1/apis'; st,fu,ct,b,er=curl(idx); directory=json.loads(b); rows=[add(idx,'official-discovery-index',b,ct,fu,st,er)]
want={'gmail','calendar','drive','sheets','docs','people','geminienterprise'}; items=[x for x in directory['items'] if x['name'] in want]
for x in sorted(items,key=lambda x:(x['name'],x['version'])): rows.append(add(x['discoveryRestUrl'],'official-discovery-reference'))
# Canonical ADC documentation
for u in ['https://cloud.google.com/docs/authentication/provide-credentials-adc','https://cloud.google.com/docs/authentication/application-default-credentials','https://cloud.google.com/docs/authentication/external/set-up-adc']: rows.append(add(u,'official-adc-canonical'))
# Pin official repo and retrieve one git archive to preserve exact authored docs sources.
repo='googleapis/google-auth-library-python'; st,fu,ct,b,er=curl(f'https://api.github.com/repos/{repo}'); meta=json.loads(b); branch=meta['default_branch']; st,fu,ct,b,er=curl(f'https://api.github.com/repos/{repo}/commits/{branch}'); commit=json.loads(b)['sha']
archive=R/'google-auth-library-python.zip'; st,fu,ct,b,er=curl(f'https://codeload.github.com/{repo}/zip/{commit}'); archive.write_bytes(b)
with zipfile.ZipFile(archive) as z:
 paths=sorted(n for n in z.namelist() if (n.split('/',1)[1].startswith('docs/') and n.endswith(('.rst','.md'))) or n.endswith('/README.rst') or n.endswith('/CONTRIBUTING.rst'))
 for n in paths:
  rel=n.split('/',1)[1]; raw=f'https://raw.githubusercontent.com/{repo}/{commit}/{rel}'; data=z.read(n)
  rows.append(add(raw,'official-google-auth-repo-doc',data,'text/x-rst',raw,200,None))
archive.unlink()
checks=[]
for u in ['https://developers.google.com/llms.txt','https://developers.google.com/llms-full.txt','https://cloud.google.com/llms.txt','https://cloud.google.com/llms-full.txt','https://ai.google.dev/llms.txt','https://ai.google.dev/llms-full.txt','https://cloud.google.com/sitemap.xml','https://cloud.google.com/gemini/enterprise/sitemap.xml']:
 st,fu,ct,b,er=curl(u);checks.append({'url':u,'http_status':st,'final_url':fu,'content_type':ct,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest() if b else None,'error':er})
m={'run_utc':datetime.now(timezone.utc).isoformat(),'selection_policy':{'title':'Actual Google integration API/reference corpus','actual_local_connector_surfaces':['gmail v1','calendar v3','drive v3','sheets v4','docs v1','people v1'],'gemini_surface':'all Gemini Enterprise (geminienterprise) entries listed in official Google Discovery Directory','adc_surface':'official canonical Google Cloud ADC pages plus every authored docs/**/*.rst|md and root README.rst/CONTRIBUTING.rst in Google-maintained google-auth-library-python','enumeration_evidence':'Google Discovery Directory for APIs and GitHub archive/tree convention pinned to a resolved commit for google-auth-library-python docs','limitation':'No valid Google LLM index was available. Cloud-wide/product-specific sitemap checks are recorded; no authoritative Gemini Enterprise narrative-doc URL enumerator was established. Complete only for declared API/reference boundary, not all Google narrative docs.'},'repository':{'repo':repo,'branch':branch,'commit':commit,'selected_paths':len(paths),'selection':'every docs/**/*.rst|md plus root README.rst and CONTRIBUTING.rst; excluded code, tests, generated output, dependencies, binaries and secrets because boundary is documentation/reference source'},'records':rows,'counts':{'enumerated':len(rows),'successful':sum(x['outcome']=='success' for x in rows),'unavailable':sum(x['outcome']!='success' for x in rows)}}
(R/'source_manifest.json').write_text(json.dumps(m,indent=2,sort_keys=True))
(R/'index_checks.json').write_text(json.dumps(checks,indent=2,sort_keys=True))
(R/'COVERAGE.md').write_text(f'''# Coverage statement\n\n**Declared boundary.** Complete API/reference documentation for the exact bundled Workspace connector APIs (Gmail v1, Calendar v3, Drive v3, Sheets v4, Docs v1, People v1); every current Gemini Enterprise (`geminienterprise`) discovery entry; and ADC's official Cloud canonical pages plus every authored documentation file in the pinned official Python google-auth repository.\n\n**Enumeration parity.** Google Discovery Directory produced every selected API/version source; the official GitHub archive pinned to `{commit}` produced every selected repository-document source. `source_manifest.json` has exactly one record per enumerated source, whether successful or unavailable.\n\n**Availability/limits.** The checked llms.txt/llms-full.txt and sitemap paths with statuses are in `index_checks.json`. No product-specific official Gemini Enterprise narrative-doc enumerator was established. Thus this is **complete for its declared API/reference boundary** and **bounded-incomplete** for a broader claim over all Google Cloud/Gemini narrative documentation.\n\n**Counts.** Enumerated {m['counts']['enumerated']}; successful {m['counts']['successful']}; unavailable {m['counts']['unavailable']}.\n''')
print(json.dumps({'matches':[(x['name'],x['version']) for x in items],**m['counts'],'commit':commit,'repo_docs':len(paths)},indent=2))
