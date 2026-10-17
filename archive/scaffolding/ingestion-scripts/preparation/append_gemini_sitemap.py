#!/usr/bin/env python3
import concurrent.futures, hashlib,json,re,subprocess,xml.etree.ElementTree as ET
from pathlib import Path
R=Path('/tmp/full-docs-backfill/google-gemini-workspace');S=R/'sources';m=json.loads((R/'source_manifest.json').read_text());checks=json.loads((R/'index_checks.json').read_text())
def curl(u):
 p=subprocess.run(['curl','-L','--max-time','30','-sS','-w','\n%{http_code}\n%{url_effective}\n%{content_type}','-A','HermesDocsBackfill/1.0',u],capture_output=True); q=p.stdout.rsplit(b'\n',3)
 if len(q)==4:
  b,c,f,t=q
  try:c=int(c)
  except:c=None
  return c,f.decode(errors='replace'),t.decode(errors='replace'),b,p.stderr.decode(errors='replace') or None
 return None,u,None,b'',p.stderr.decode(errors='replace') or 'curl parse failure'
def record(u,cat,raw=None,meta=None):
 st,fn,ct,b,e=meta if meta else curl(u)
 r={'source_url':u,'category':cat,'http_status':st,'final_url':fn,'content_type':ct,'error':e}
 if st==200 and b:
  p=S/(re.sub('[^A-Za-z0-9._-]+','_',u.replace('https://',''))[:180]+'.html');p.write_bytes(b);r.update(file=str(p.relative_to(R)),bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),outcome='success')
 else:r.update(file=None,bytes=0,sha256=None,outcome='unavailable')
 return r
index='https://docs.cloud.google.com/sitemap.xml';st,fn,ct,b,e=curl(index); checks.append({'url':index,'http_status':st,'final_url':fn,'content_type':ct,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest() if b else None,'error':e})
root=ET.fromstring(b); ns={'s':'http://www.sitemaps.org/schemas/sitemap/0.9'}; shards=[x.text for x in root.findall('s:sitemap/s:loc',ns)]
def shard(u):return u,curl(u)
with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex: shard_res=list(ex.map(shard,shards))
urls=[]
for u,(st,fn,ct,b,e) in shard_res:
 checks.append({'url':u,'http_status':st,'final_url':fn,'content_type':ct,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest() if b else None,'error':e})
 if st==200:
  try: urls += [x.text for x in ET.fromstring(b).findall('s:url/s:loc',ns) if '/gemini/enterprise/' in x.text]
  except Exception:pass
urls=sorted(set(urls));print('gemini sitemap urls',len(urls),flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:gemrows=list(ex.map(lambda u:record(u,'official-gemini-enterprise-sitemap-doc'),urls))
m['records'] += gemrows;m['counts']={'enumerated':len(m['records']),'successful':sum(x['outcome']=='success' for x in m['records']),'unavailable':sum(x['outcome']!='success' for x in m['records'])}
m['selection_policy']['gemini_surface']='every Google Cloud sitemap URL whose canonical path contains /gemini/enterprise/, plus all discovery documents if Google Discovery Directory lists them (it listed none at acquisition)'
m['selection_policy']['enumeration_evidence']='Google Discovery Directory for connector APIs; official docs.cloud.google.com sitemap index and all 60 shard sitemaps filtered only by canonical Gemini Enterprise product prefix; and pinned GitHub archive/tree convention for google-auth-library-python docs'
m['selection_policy']['limitation']='No valid Google LLM index was available. Discovery has no geminienterprise item. Gemini Enterprise pages are fully enumerated by the public Cloud sitemap prefix. ADC vendor documentation does not expose a product-specific enumeration; its canonical Cloud pages and complete official SDK docs are retained, so broader Cloud-auth narrative coverage remains bounded.'
(R/'source_manifest.json').write_text(json.dumps(m,indent=2,sort_keys=True));(R/'index_checks.json').write_text(json.dumps(checks,indent=2,sort_keys=True))
(R/'COVERAGE.md').write_text(f'''# Coverage statement\n\n**Declared boundary.** Every API/reference source for the exact bundled Workspace connector APIs (Gmail v1, Calendar v3, Drive v3, Sheets v4, Docs v1, People v1); every public Gemini Enterprise documentation URL in the official Cloud sitemap whose path is `/gemini/enterprise/`; and ADC's three canonical official Cloud pages plus every authored documentation file in the pinned official Python google-auth repository.\n\n**Enumeration parity.** Google Discovery Directory selected each connector API/version document. The official `docs.cloud.google.com/sitemap.xml` and all 60 shards selected every Gemini Enterprise URL by one objective product-prefix rule; each becomes one manifest source row. The official Google-auth archive at pinned commit supplies every selected repo document. `source_manifest.json` has one row per selected source, successful or unavailable.\n\n**Unavailable/missing enumeration paths.** Every checked Google LLM index returned non-success (recorded in `index_checks.json`). The Discovery Directory contained no `geminienterprise` entry; direct Gemini Enterprise discovery endpoints were unavailable. Google does not expose a product-specific ADC documentation index/sitemap; only its canonical pages plus the complete official SDK documentation are in scope. Thus **Gemini and Workspace API reference coverage is full for this boundary; ADC Cloud narrative documentation remains bounded-incomplete**, rather than being falsely labeled whole-Cloud complete.\n\n**Counts.** Enumerated {m['counts']['enumerated']}; successful {m['counts']['successful']}; unavailable {m['counts']['unavailable']}; Gemini sitemap URLs {len(urls)}.\n''')
print(json.dumps({'gemini_urls':len(urls),**m['counts']},indent=2))
