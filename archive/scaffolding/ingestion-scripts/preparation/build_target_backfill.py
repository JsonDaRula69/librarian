#!/usr/bin/env python3
"""Read-only corpus acquisition for OpenCode and SisterSage."""
import concurrent.futures, hashlib, json, os, re, shutil, subprocess, time
from pathlib import Path
from urllib.parse import urlsplit
import requests
import xml.etree.ElementTree as ET

ROOT=Path('/tmp/full-docs-backfill')
OC=ROOT/'opencode-core'; SS=ROOT/'sistersage'
NOW=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
UA='Mozilla/5.0 (compatible; Hermes full-docs-backfill/1.0)'
SISTER='/Users/djtchill/Documents/Projects/SisterSage'
# Configuration, source, and authored documentation: all are source-of-truth when text.
TEXT_EXT={'.md','.mdx','.txt','.json','.jsonc','.yaml','.yml','.toml','.ts','.tsx','.js','.mjs','.cjs','.sh','.py','.css','.html','.htm','.xml','.ini','.conf','.lock'}
EXACT={'README','LICENSE','NOTICE','Makefile','Dockerfile','package.json','package-lock.json','tsconfig.json','opencode.json','oh-my-openagent.json','oh-my-openagent.json.NoGPT.json','.gitignore'}
SECRET_NAME=re.compile(r'(^|/)(\.env(?:\..*)?|.*(?:secret|credential|token|apikey|api_key|password|private).*)$',re.I)
SECRET_TEXT=re.compile(r'(?:api[_-]?key|secret|password|token|authorization)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{12,}',re.I)
def run(cmd, **kw): return subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,**kw)
def sha(b): return hashlib.sha256(b).hexdigest()
def clean(p):
    if p.exists(): raise SystemExit(f'Refusing to overwrite immutable target: {p}')
def write_json(p,o): p.write_text(json.dumps(o,indent=2,ensure_ascii=False)+'\n')
def safe_name(i,url):
    slug=re.sub(r'[^A-Za-z0-9]+','-',urlsplit(url).path.strip('/') or 'docs-index').strip('-')[:100] or 'docs-index'
    return f'{i:04d}_{slug}_{sha(url.encode())[:12]}.html'
def get(url):
    # curl's HTTP/2 + compression path is accepted by opencode.ai's CDN where requests is challenged.
    try:
      cp=subprocess.run(['curl','-L','--compressed','-sS','--max-time','45','-w','\\n__HERMES_META__%{http_code}\\t%{url_effective}\\t%{content_type}','-o','-','--',url],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
      marker=b'\n__HERMES_META__'; body,meta=cp.stdout.rsplit(marker,1)
      status,final_url,content_type=meta.decode('utf-8','replace').split('\t',2)
      return {'status':int(status),'final_url':final_url,'content_type':content_type,'body':body}
    except Exception as e: return {'status':None,'final_url':url,'content_type':None,'error':type(e).__name__+': '+str(e)}
def opencode():
    clean(OC); (OC/'sources').mkdir(parents=True); (OC/'indexes').mkdir()
    checks=[]
    for u in ['https://opencode.ai/llms.txt','https://opencode.ai/llms-full.txt','https://opencode.ai/sitemap.xml','https://opencode.ai/docs/sitemap.xml']:
      r=get(u); rec={'url':u,'status':r['status'],'final_url':r['final_url'],'content_type':r['content_type'],'bytes':len(r.get('body',b''))}
      if r.get('body'):
        f=OC/'indexes'/({'https://opencode.ai/sitemap.xml':'root-sitemap.xml','https://opencode.ai/docs/sitemap.xml':'docs-sitemap.xml'}.get(u,u.rsplit('/',1)[1])); f.write_bytes(r['body']); rec['file']=str(f.relative_to(OC)); rec['sha256']=sha(r['body'])
      if 'error' in r: rec['error']=r['error']
      checks.append(rec)
    sm=next(x for x in checks if x['url']=='https://opencode.ai/sitemap.xml')
    xml=(OC/sm['file']).read_bytes(); locs=sorted(set(e.text.strip() for e in ET.fromstring(xml).iter() if e.tag.endswith('loc') and e.text))
    # Declared full policy: every canonical opencode.ai sitemap location under /docs, plus /docs itself.
    urls=[u for u in locs if urlsplit(u).netloc=='opencode.ai' and (urlsplit(u).path=='/docs' or urlsplit(u).path.startswith('/docs/'))]
    def one(a):
      i,u=a; r=get(u); rec={'ordinal':i,'listed_url':u,'canonical_url':r['final_url'],'http_status':r['status'],'status':'unavailable'}
      if r.get('body') and r['status']==200:
        f=OC/'sources'/safe_name(i,u); f.write_bytes(r['body']); rec.update({'status':'success','file':str(f.relative_to(OC)),'bytes':len(r['body']),'sha256':sha(r['body']),'content_type':r['content_type'],'utf8_decodable':True})
        try:r['body'].decode('utf-8')
        except UnicodeDecodeError:rec['utf8_decodable']=False
      else: rec['error']=r.get('error')
      return rec
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex: records=list(ex.map(one,enumerate(urls,1)))
    ok=sum(x['status']=='success' for x in records)
    m={'schema':'full-docs-backfill-source-manifest-v2','created_at_utc':NOW,'corpus':'opencode-core','canonical_source':'https://opencode.ai/sitemap.xml','selection_policy':'Full policy: parse every <loc> in the downloaded official opencode.ai sitemap; select every URL on opencode.ai whose path is /docs or begins /docs/. This is the complete official documentation/config/schema/manifest page surface represented by the canonical sitemap at acquisition time; non-/docs marketing, blog, and legal pages are enumerated by the sitemap but deliberately outside the documentation corpus policy. llms indexes were checked and recorded but were unavailable.','index_checks':checks,'sitemap_total_locations':len(locs),'selected_locations':len(urls),'index_parity':{'expected':len(urls),'records':len(records),'parity':len(urls)==len(records)},'sources':records,'coverage_claim':f'All {len(records)} selected official sitemap documentation URLs have explicit records: {ok} collected, {len(records)-ok} unavailable. Full-policy collection is '+('complete.' if ok==len(records) else 'incomplete due to recorded unavailable URLs.')}
    write_json(OC/'provenance-manifest.json',m); (OC/'COVERAGE.md').write_text('# OpenCode core coverage\n\n'+m['selection_policy']+'\n\n'+m['coverage_claim']+'\n')
def ssh(args,input=None): return run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=20','macbook']+args,input=input)
def sistersage():
    clean(SS); (SS/'sources').mkdir(parents=True)
    commit=ssh([f'git -C {SISTER} rev-parse HEAD']).stdout.decode().strip()
    tracked=set(ssh([f'git -C {SISTER} ls-tree -r --name-only {commit}']).stdout.decode('utf-8','surrogateescape').splitlines())
    # Enumerate complete filesystem candidate set; no mutation. NUL makes unusual names safe.
    remote=f"cd {SISTER} && find . -type f -print0"
    paths=ssh([remote]).stdout.decode('utf-8','surrogateescape').split('\0')
    records=[]; selected=[]
    for rp in sorted(p for p in paths if p):
      p=rp[2:] if rp.startswith('./') else rp
      reason=None; include=False
      low=p.lower(); suf=Path(p).suffix.lower(); base=Path(p).name
      if '/.git/' in '/'+p or p=='.git': reason='excluded: Git metadata is not first-party corpus content'
      elif '/node_modules/' in '/'+p: reason='excluded: dependency vendor content'
      elif base=='.DS_Store' or suf in {'.png','.jpg','.jpeg','.gif','.ico','.pdf','.zip','.gz','.dmg','.app','.bin'}: reason='excluded: generated binary/non-text artifact'
      elif p.startswith('.omo/run-continuation/') or p.startswith('data/') or p.startswith('debug-') or p.startswith('tool-failures') or p.endswith('.jsonl'): reason='excluded: generated runtime/session/log artifact (not authored source-of-truth)'
      elif SECRET_NAME.search(p): reason='excluded: secret-bearing filename risk; contents not read'
      elif suf in TEXT_EXT or base in EXACT: include=True; reason='included: first-party authored documentation, configuration/manifest, or executable source defining plugin operation behavior'
      else: reason='excluded: non-text/unclassified artifact; no content copied'
      rec={'path':p,'selection': 'included' if include else 'excluded','reason':reason}
      if include: selected.append((p,rec))
      records.append(rec)
    # Screen selected file contents remotely without output, then stream only safe selected files and verify remote/local hashes.
    for i,(p,rec) in enumerate(selected,1):
      q=p.replace("'","'\\''")
      check=ssh([f"python3 -c \"import re,sys; b=open(sys.argv[1],'rb').read(); print('SECRET' if re.search(rb'(api[_-]?key|secret|password|token|authorization)\\s*[=:]\\s*[\\\\\\\"\\\']?[A-Za-z0-9_-]{{12,}}',b,re.I) else 'OK')\" '{SISTER}/{q}'"]).stdout.decode().strip()
      if check!='OK': rec.update({'selection':'excluded','reason':'excluded: secret-like value detected by remote content screen; contents not copied'}); continue
      if p in tracked:
        b=ssh([f"git -C {SISTER} show {commit}:'{q}'"]).stdout
        snapshot='git-tracked'
      else:
        b=ssh([f"cat '{SISTER}/{q}'"]).stdout
        snapshot='untracked-file-snapshot'
      # The remote local project is authoritative for untracked authored source too; label its snapshot honestly.
      local=SS/'sources'/f'{i:04d}_{p.replace("/","__")}.txt'; local.parent.mkdir(parents=True,exist_ok=True); local.write_bytes(b)
      rec.update({'git_commit':commit if p in tracked else None,'snapshot_kind':snapshot,'source_url':f'ssh://macbook{SISTER}/{p}' + (f'@{commit}' if p in tracked else ''),'file':str(local.relative_to(SS)),'bytes':len(b),'sha256':sha(b),'utf8_decodable':True})
      try:b.decode('utf-8')
      except UnicodeDecodeError: rec['selection']='excluded'; rec['reason']='excluded: generated binary/non-UTF-8 artifact'; local.unlink(); rec.pop('file'); rec['utf8_decodable']=False
    included=[x for x in records if x['selection']=='included']
    excluded=[x for x in records if x['selection']=='excluded']
    m={'schema':'full-docs-backfill-source-manifest-v2','created_at_utc':NOW,'corpus':'sistersage','canonical_source':f'read-only SSH macbook:{SISTER} at git commit {commit}','selection_policy':'Full policy: enumerate every regular file under the first-party SisterSage project root through read-only SSH; retain every UTF-8 authored documentation, configuration/manifest, and executable source file that can define plugin operation behavior, whether Git-tracked or a labeled untracked local snapshot. Exclude only Git metadata, node_modules/vendor dependencies, generated binaries/non-text artifacts, generated runtime/session/log data, and suspected secrets; every exclusion is individually recorded. Local .local/share/sistersage was inspected as runtime state and its log artifacts are excluded by the generated-runtime rule.','git_commit':commit,'enumerated_regular_files':len(records),'selected_files':len(included),'excluded_files':len(excluded),'index_parity':{'expected':len(included),'records_with_local_source':sum('file'in x for x in included),'parity':len(included)==sum('file'in x for x in included)},'sources':records,'coverage_claim':f'Complete first-party project-root enumeration: {len(records)} regular files classified; {len(included)} safe UTF-8 source-of-truth artifacts collected with hashes; {len(excluded)} exclusions individually documented.'}
    write_json(SS/'provenance-manifest.json',m); (SS/'COVERAGE.md').write_text('# SisterSage coverage\n\n'+m['selection_policy']+'\n\n'+m['coverage_claim']+'\n')
if __name__=='__main__': opencode(); sistersage()
