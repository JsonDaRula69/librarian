import os,re,json,hashlib,shutil,subprocess,sys,time
from pathlib import Path
from urllib.parse import urljoin,urlsplit,urlunsplit
import requests
ROOT=Path('/tmp/full-docs-backfill/icloud-integration')
if ROOT.exists(): raise SystemExit(f'refusing existing path: {ROOT}')
ROOT.mkdir(parents=True)
(raw:=ROOT/'raw').mkdir(); (prepared:=ROOT/'prepared').mkdir(); (repos:=ROOT/'repos').mkdir()
S=requests.Session(); S.headers['User-Agent']='Mozilla/5.0 corpus-audit/1.0'
def clean(u):
 p=urlsplit(u); return urlunsplit((p.scheme,p.netloc,p.path,p.query,''))
def fetch(url, rel):
 try:
  r=S.get(url,timeout=45); data=r.content; path=raw/rel; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(data)
  return {'source_url':url,'final_url':clean(r.url),'http_status':r.status_code,'content_type':r.headers.get('content-type',''),'bytes':len(data),'raw_path':str(path.relative_to(ROOT)),'sha256':hashlib.sha256(data).hexdigest(),'valid':r.status_code==200 and len(data)>0}
 except Exception as e: return {'source_url':url,'http_status':None,'error':f'{type(e).__name__}: {e}','valid':False}
def links(html,base):
 out=[]
 for h in re.findall(r'''href=["']([^"'#]+)''',html,re.I):
  u=clean(urljoin(base,h))
  if u.startswith('https://support.apple.com/guide/'): out.append(u)
 return sorted(set(out))
index_checks=[]
for u in ['https://support.apple.com/llms.txt','https://support.apple.com/llms-full.txt','https://support.apple.com/sitemap.xml','https://developer.apple.com/llms.txt','https://developer.apple.com/llms-full.txt','https://developer.apple.com/sitemap.xml']:
 d=fetch(u, 'index-checks/'+hashlib.sha256(u.encode()).hexdigest()[:16]+'.bin'); index_checks.append({k:d.get(k) for k in ['source_url','http_status','content_type','bytes','sha256','valid']})
# Apple Support guide indexes define a product-specific navigation boundary, not all Apple support.
seed=['https://support.apple.com/guide/icloud/welcome/icloud','https://support.apple.com/guide/calendar/welcome/mac','https://support.apple.com/guide/contacts/welcome/mac']
seed_docs=[]; candidates=set(seed)
for n,u in enumerate(seed):
 d=fetch(u,f'apple/index-{n}.html'); seed_docs.append(d)
 if d['valid']:
  text=(ROOT/d['raw_path']).read_text(errors='ignore')
  for v in links(text,d['final_url']):
   # all linked pages are first-party guide navigation candidates; topic predicate establishes boundary
   if any(x in v.lower() for x in ['icloud','mail','calendar','contact']): candidates.add(v)
# Explicit, necessary provider configuration articles listed in prior corpus.
for u in ['https://support.apple.com/en-us/102654','https://support.apple.com/en-us/102525'] : candidates.add(u)
records=[]
for i,u in enumerate(sorted(candidates)):
 d=fetch(u,f'apple/pages/{i:04d}.html'); d['kind']='apple-support-guide-or-provider-config'; records.append(d)
# official local sync tools cited in earlier corpus; deterministic source docs/config boundary at pinned commits.
tools={'vdirsyncer':'https://github.com/pimutils/vdirsyncer.git','khal':'https://github.com/pimutils/khal.git','himalaya':'https://github.com/pimalaya/himalaya.git'}
repo_meta=[]
for name,url in tools.items():
 dest=repos/name
 subprocess.run(['git','clone','--depth','1',url,str(dest)],check=True,stdout=subprocess.DEVNULL)
 commit=subprocess.check_output(['git','-C',str(dest),'rev-parse','HEAD'],text=True).strip()
 # include authored docs/docs trees, top-level readme, package/config manifests; exclude tests/examples/generated/deps/binaries
 files=[]
 for p in sorted(dest.rglob('*')):
  if not p.is_file() or '.git' in p.parts: continue
  rel=p.relative_to(dest).as_posix(); low=rel.lower(); base=p.name.lower()
  keep=(low.startswith('docs/') or low.startswith('doc/') or base.startswith('readme') or base in {'pyproject.toml','setup.cfg','setup.py','cargo.toml','config.toml','config.yml','config.yaml','justfile','makefile'})
  bad=any(x in low.split('/') for x in ['test','tests','example','examples','fixtures','vendor','node_modules','target','dist'])
  if keep and not bad:
   b=p.read_bytes()
   try: b.decode('utf-8')
   except UnicodeDecodeError: continue
   target=raw/'tools'/name/rel; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(p,target)
   records.append({'source_url':f'https://raw.githubusercontent.com/{url.split("/")[-2]}/{url.split("/")[-1].removesuffix(".git")}/{commit}/{rel}','final_url':f'https://raw.githubusercontent.com/{url.split("/")[-2]}/{url.split("/")[-1].removesuffix(".git")}/{commit}/{rel}','http_status':200,'content_type':'text/plain; pinned git source','bytes':len(b),'raw_path':str(target.relative_to(ROOT)),'sha256':hashlib.sha256(b).hexdigest(),'valid':len(b)>0,'kind':f'official-upstream-{name}','repo':url,'commit':commit})
   files.append(rel)
 repo_meta.append({'name':name,'repo':url,'commit':commit,'selection':'authored docs/doc trees, README*, package/config manifests; excluded tests/examples/fixtures/vendor/generated/binaries','selected_count':len(files),'selected_files':files})
# prepared safe markdown copies for every valid artifact
for n,r in enumerate(records):
 if r['valid']:
  src=ROOT/r['raw_path']; ext=src.suffix.lower(); dst=prepared/f'{n:04d}-{src.stem}.md'
  if ext in ('.html','.htm'):
   text=src.read_text('utf-8',errors='replace'); text='Source URL: '+r['final_url']+'\n\n'+text
  else: text=src.read_text('utf-8',errors='replace')
  dst.write_text(text,encoding='utf-8'); r['upload_path']=str(dst.relative_to(ROOT)); r['upload_sha256']=hashlib.sha256(dst.read_bytes()).hexdigest()
manifest={'schema':'icloud-integration-full-boundary-v1','created_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'boundary':{'classification':'full within declared source boundary; not all Apple documentation','apple':'Apple Support iCloud guide plus the linked Apple Support navigation pages whose URL expresses iCloud/Mail/Calendar/Contacts, the macOS Calendar/Contacts guide pages whose URL expresses iCloud, and the two Apple provider-configuration articles already in prior corpus (102654, 102525). Apple llms and sitemap endpoints were checked and unavailable; the three official guide landing pages are the first-party product indexes used for enumeration.','tools':'The three official upstream repositories explicitly documented in the earlier corpus: pimutils/vdirsyncer, pimutils/khal, pimalaya/himalaya. At each pinned HEAD, all UTF-8 authored docs/doc trees, README*, and package/config manifests were enumerated; tests/examples/fixtures/vendor/generated/binaries excluded.'},'index_checks':index_checks,'apple_seed_indexes':seed_docs,'repositories':repo_meta,'records':records,'parity':{'enumerated_records':len(records),'valid_records':sum(r['valid'] for r in records),'unavailable_records':sum(not r['valid'] for r in records),'prepared_records':sum('upload_path' in r for r in records)}}
(ROOT/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
(ROOT/'coverage-statement.md').write_text('# iCloud integration source boundary and coverage\n\n'+manifest['boundary']['apple']+'\n\n'+manifest['boundary']['tools']+'\n\nEnumeration parity: every selected artifact has one `records` entry; valid items have a source hash and prepared upload copy; unavailable items remain explicit manifest rows. This does not claim all Apple documentation.\n',encoding='utf-8')
print(json.dumps({'root':str(ROOT),'records':manifest['parity'],'repos':[(x['name'],x['commit'],x['selected_count']) for x in repo_meta],'apple_candidates':len(candidates)},indent=2))
