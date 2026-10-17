#!/usr/bin/env python3
"""Download Voyage AI docs enumerated in official llms.txt and retain them to local Hindsight."""
from pathlib import Path
from urllib.parse import urlparse
import hashlib, json, os, re, sys, time
import requests

ROOT = Path('/tmp/voyageai_docs')
DOCS = ROOT / 'files'
MANIFEST = ROOT / 'manifest.json'
INDEX_URL = 'https://docs.voyageai.com/llms.txt'
API = 'http://127.0.0.1:8888/v1/default'
ENV = Path('/home/jsondarula/.hermes/.env')
ROOT.mkdir(parents=True, exist_ok=True); DOCS.mkdir(parents=True, exist_ok=True)

def env_value(key):
    if not ENV.exists(): return None
    pat = re.compile(r'^\s*(?:export\s+)?' + re.escape(key) + r'\s*=\s*(.*?)\s*$')
    for line in ENV.read_text(errors='replace').splitlines():
        m=pat.match(line)
        if m:
            v=m.group(1).strip()
            if len(v)>=2 and v[0] == v[-1] and v[0] in "\"'": v=v[1:-1]
            return v
    return None

def headers():
    # Support common local naming while never rendering the resulting secret.
    key = next((env_value(k) for k in ('HINDSIGHT_API_KEY','HINDSIGHT_KEY','HINDSIGHT_API_TOKEN') if env_value(k)), None)
    return {'Authorization': f'Bearer {key}'} if key else {}

def clean_url(u):
    return u.strip().rstrip(').,;:>')

def parse_urls(txt):
    # llms.txt uses Markdown links and possibly bare URLs. Scope to docs host, omit index itself.
    found=[]
    for u in re.findall(r'\[[^\]]*\]\((https?://[^\s)]+)\)|(?<!\]\()(?<!\()(?<![\w/])(https?://[^\s<>]+)', txt):
        u=clean_url(u[0] or u[1])
        p=urlparse(u)
        if p.netloc == 'docs.voyageai.com' and u != INDEX_URL: found.append(u)
    return list(dict.fromkeys(found))

def safe_filename(url, n):
    p=urlparse(url)
    stem=(p.path.strip('/').replace('/','__') or 'index')
    stem=re.sub(r'[^A-Za-z0-9._-]+','_',stem).strip('._') or 'index'
    # Keep a semantic extension for parser. Listed endpoints are Markdown but URL paths often extensionless.
    if not stem.endswith(('.md','.markdown','.txt','.html')): stem += '.md'
    return f'{n:03d}_{stem}'

def download():
    s=requests.Session(); s.headers['User-Agent']='Liv-docs-ingestion/1.0'
    idx=s.get(INDEX_URL,timeout=60)
    idx.raise_for_status()
    (ROOT/'llms.txt').write_bytes(idx.content)
    urls=parse_urls(idx.text)
    records=[]
    for n,source in enumerate(urls,1):
        rec={'source_url':source,'attempts':[],'download_status':None,'recovery':None}
        candidates=[('original',source)]
        p=urlparse(source)
        if p.path.endswith('.md'):
            candidates.append(('canonical_extensionless',p._replace(path=p.path[:-3]).geturl()))
        for kind,u in candidates:
            try:
                r=s.get(u,timeout=60,allow_redirects=True)
                attempt={'kind':kind,'url':u,'http_status':r.status_code,'final_url':r.url,'content_type':r.headers.get('content-type',''),'bytes':len(r.content)}
                rec['attempts'].append(attempt)
                ctype=r.headers.get('content-type','').lower()
                # Require successful non-HTML content, except extensionless canonical pages may be public HTML docs.
                acceptable=r.status_code==200 and (kind=='canonical_extensionless' or 'html' not in ctype or r.url.endswith(('.md','.txt')))
                if acceptable:
                    name=safe_filename(source,n); target=DOCS/name
                    target.write_bytes(r.content)
                    # Ensure text is UTF-8-compatible for markdown parsers; source docs are UTF-8.
                    try: target.read_text(encoding='utf-8')
                    except UnicodeDecodeError:
                        target.unlink(missing_ok=True); rec['download_status']='failed_non_utf8'; break
                    rec.update({'download_status':'downloaded' if kind=='original' else 'canonical_recovered','final_url':r.url,'filename':name,'local_path':str(target),'byte_count':len(r.content),'sha256':hashlib.sha256(r.content).hexdigest(),'content_type':ctype,'recovery':kind if kind!='original' else None})
                    break
            except Exception as e:
                rec['attempts'].append({'kind':kind,'url':u,'error':f'{type(e).__name__}: {str(e)[:300]}'})
        if not rec['download_status']: rec['download_status']='failed'
        records.append(rec)
        print(f'DOWNLOAD {n}/{len(urls)} {rec["download_status"]}', flush=True)
    manifest={'vendor':'Voyage AI','index_url':INDEX_URL,'index_final_url':idx.url,'index_http_status':idx.status_code,'index_bytes':len(idx.content),'source_count':len(urls),'records':records}
    MANIFEST.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return manifest

def summary(m):
    rec=m['records']; d=[r for r in rec if r['download_status'] in ('downloaded','canonical_recovered')]
    return {'sources':len(rec),'downloaded':len(d),'canonical_recovered':sum(r['download_status']=='canonical_recovered' for r in rec),'failed':len(rec)-len(d)}

if __name__=='__main__':
    m=download(); print('MANIFEST='+str(MANIFEST)); print('SUMMARY='+json.dumps(summary(m)))
