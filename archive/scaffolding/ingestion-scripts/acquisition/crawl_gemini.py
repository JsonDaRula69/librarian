#!/usr/bin/env python3
import concurrent.futures,hashlib,json,re,subprocess
from html.parser import HTMLParser
from pathlib import Path
R=Path('/tmp/full-docs-backfill/google-gemini-workspace');S=R/'sources';m=json.loads((R/'source_manifest.json').read_text())
def curl(u):
 p=subprocess.run(['curl','-L','--max-time','25','-sS','-w','\n%{http_code}\n%{url_effective}\n%{content_type}','-A','HermesDocsBackfill/1.0',u],capture_output=True);q=p.stdout.rsplit(b'\n',3)
 if len(q)==4:
  b,c,f,t=q
  try:c=int(c)
  except:c=None
  return c,f.decode(errors='replace'),t.decode(errors='replace'),b,p.stderr.decode(errors='replace') or None
 return None,u,None,b'',p.stderr.decode(errors='replace') or 'curl parse failure'
class P(HTMLParser):
 def __init__(self):super().__init__();self.links=[]
 def handle_starttag(self,t,a):
  if t=='a':
   d=dict(a);self.links.append(d.get('href',''))
def norm(x):
 x=x.split('#')[0].split('?')[0]
 if x.startswith('/') : x='https://docs.cloud.google.com'+x
 if x.startswith('https://cloud.google.com/'):x='https://docs.cloud.google.com/'+x.split('cloud.google.com/',1)[1]
 return x.rstrip('/')
def crawl_fetch(u):return u,curl(u)
seen=set();front={'https://docs.cloud.google.com/gemini/enterprise/docs'};cache={}
for depth in range(5):
 batch=sorted(front-seen);seen.update(batch);front=set()
 if not batch:break
 with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:out=list(ex.map(crawl_fetch,batch))
 for u,r in out:
  cache[u]=r
  if r[0]==200:
   p=P();p.feed(r[3].decode(errors='replace'))
   for h in p.links:
    v=norm(h)
    if v.startswith('https://docs.cloud.google.com/gemini/enterprise/') and len(v)<250:front.add(v)
print('gemini navigation urls',len(seen),flush=True)
old={r['source_url'] for r in m['records']};add=[]
for u in sorted(seen-old):
 st,fn,ct,b,e=cache[u];r={'source_url':u,'category':'official-gemini-enterprise-navigation','http_status':st,'final_url':fn,'content_type':ct,'error':e}
 if st==200 and b:
  p=S/(re.sub('[^A-Za-z0-9._-]+','_',u.replace('https://',''))[:180]+'.html');p.write_bytes(b);r.update(file=str(p.relative_to(R)),bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),outcome='success')
 else:r.update(file=None,bytes=0,sha256=None,outcome='unavailable')
 add.append(r)
m['records']+=add;m['counts']={'enumerated':len(m['records']),'successful':sum(x['outcome']=='success' for x in m['records']),'unavailable':sum(x['outcome']!='success' for x in m['records'])}
m['selection_policy']['gemini_surface']='all canonical Gemini Enterprise pages reachable by breadth-first crawl (five navigation depths) from the official product documentation index, restricted to https://docs.cloud.google.com/gemini/enterprise/; public sitemap shards contained no matching URLs.'
m['selection_policy']['limitation']='No valid Google LLM index was available. Discovery has no geminienterprise item and Cloud sitemap shards had no Gemini Enterprise paths. The fallback official product-navigation crawl is fully recorded but cannot prove an independently enumerable entire product set. ADC vendor documentation also lacks a product-specific enumerator; broader Cloud-auth narrative coverage is bounded.'
(R/'source_manifest.json').write_text(json.dumps(m,indent=2,sort_keys=True))
(R/'COVERAGE.md').write_text(f'''# Coverage statement\n\n**Declared boundary.** Every API/reference source for the exact bundled Workspace connector APIs (Gmail v1, Calendar v3, Drive v3, Sheets v4, Docs v1, People v1); every Gemini Enterprise page reached from the official product-documentation navigation index within five product-scoped link depths; and ADC's three canonical official Cloud pages plus every authored documentation file in the pinned official Python google-auth repository.\n\n**Enumeration parity.** Google Discovery Directory supplied each connector API/version document. Gemini Enterprise's official navigation graph supplied {len(seen)} product-prefix URLs, with one source-manifest record per URL. The Discovery Directory had no Gemini Enterprise entry and the 60 public sitemap shards yielded no matching product-prefix URLs; both facts are preserved in `index_checks.json`. The official Google-auth archive supplies every selected repo document.\n\n**Classification.** Workspace API reference is complete for this boundary. Gemini navigation and ADC Cloud narrative docs remain **bounded-incomplete** because Google did not expose an independent product-specific URL enumerator; this corpus does not claim whole-Cloud/Gemini narrative coverage.\n\n**Counts.** Enumerated {m['counts']['enumerated']}; successful {m['counts']['successful']}; unavailable {m['counts']['unavailable']}; Gemini navigation URLs {len(seen)}.\n''')
print(json.dumps({'new_gemini':len(add),'gemini_navigation_urls':len(seen),**m['counts']},indent=2))
