import os, re, json, hashlib, time, urllib.request, urllib.error
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
ROOT=Path('/tmp/full-docs-backfill')
PRODUCTS={
 'hindsight': 'https://hindsight.vectorize.io',
 'ollama': 'https://docs.ollama.com',
}
UA='Mozilla/5.0 HermesDocsBackfill/1.0'
def fetch(url):
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'text/markdown,text/plain,text/html;q=0.9,*/*;q=0.1'})
    try:
        with urllib.request.urlopen(req,timeout=45) as r:
            b=r.read(); return {'status':r.status,'final_url':r.url,'content_type':r.headers.get_content_type(),'body':b}
    except urllib.error.HTTPError as e:
        return {'status':e.code,'final_url':e.geturl(),'content_type':e.headers.get_content_type() if e.headers else None,'error':str(e)}
    except Exception as e: return {'status':None,'final_url':url,'content_type':None,'error':repr(e)}
def urls_from(text, domain):
    raw=re.findall(r'https?://[^\s<>"\']+',text)
    out=[]
    for u in raw:
        u=u.rstrip('.,;:!?)\]}\'"')
        p=urlsplit(u)
        if p.netloc==domain:
            out.append(urlunsplit((p.scheme,p.netloc,p.path,p.query,'')))
    return sorted(set(out))
def safe_name(url, n, content_type):
    p=urlsplit(url).path.rsplit('/',1)[-1] or 'index'
    p=re.sub(r'[^A-Za-z0-9._-]+','_',p)[:100]
    suf=Path(p).suffix.lower()
    if suf not in ('.md','.markdown','.html','.htm','.txt'):
        suf='.html' if content_type=='text/html' else '.md'
    return f'{n:04d}_{p[:80-len(suf)]}_{hashlib.sha256(url.encode()).hexdigest()[:12]}{suf}'
def main():
 for product,base in PRODUCTS.items():
    root=ROOT/product; sources=root/'sources'; indexes=root/'indexes'
    if root.exists(): raise SystemExit(f'Refusing existing immutable corpus: {root}')
    sources.mkdir(parents=True); indexes.mkdir()
    domain=urlsplit(base).netloc
    checks=[]; allurls=[]
    for kind in ('llms.txt','llms-full.txt'):
        u=base+'/'+kind; r=fetch(u)
        rec={'kind':kind,'url':u,'status':r['status'],'final_url':r['final_url'],'content_type':r['content_type'],'bytes':len(r.get('body',b''))}
        if 'error' in r: rec['error']=r['error']
        if r.get('body'):
            fn=indexes/kind; fn.write_bytes(r['body']); rec['file']=str(fn.relative_to(root)); rec['sha256']=hashlib.sha256(r['body']).hexdigest()
            # An HTML response to llms endpoint is not a valid document index.
            rec['valid_document_index']=r['status']==200 and r['content_type']!='text/html'
            if rec['valid_document_index']:
                us=urls_from(r['body'].decode('utf-8','replace'),domain); rec['enumerated_url_count']=len(us); allurls+=us
            else: rec['enumerated_url_count']=0
        else: rec['valid_document_index']=False; rec['enumerated_url_count']=0
        checks.append(rec)
    urls=sorted(set(allurls))
    records=[]
    for i,u in enumerate(urls,1):
        r=fetch(u); rec={'ordinal':i,'listed_url':u,'canonical_url':r['final_url'],'initial_status':r['status'],'final_status':r['status'],'recovery_attempted':False,'status':'unavailable'}
        if r.get('body') and r['status']==200:
            rec['status']='success'; body=r['body']; ct=r['content_type']
        elif urlsplit(u).path.endswith('.md') and r['status']==404:
            rec['recovery_attempted']=True
            p=urlsplit(u); can=urlunsplit((p.scheme,p.netloc,p.path[:-3],p.query,'')); rr=fetch(can)
            rec['recovery_url']=can; rec['recovery_status']=rr['status']; rec['canonical_url']=rr['final_url']; rec['final_status']=rr['status']
            if rr.get('body') and rr['status']==200:
                rec['status']='recovered'; body=rr['body']; ct=rr['content_type']
            else:
                rec['error']=rr.get('error')
        else: rec['error']=r.get('error')
        if rec['status'] in ('success','recovered'):
            fn=safe_name(rec['canonical_url'],i,ct); (sources/fn).write_bytes(body)
            rec.update({'file':str((sources/fn).relative_to(root)),'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),'content_type':ct,'utf8_decodable':True})
            try: body.decode('utf-8')
            except UnicodeDecodeError: rec['utf8_decodable']=False
        records.append(rec)
    success=sum(x['status'] in ('success','recovered') for x in records)
    unavailable=len(records)-success
    manifest={'schema':'full-docs-backfill-source-manifest-v1','product':product,'created_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'official_base':base,'index_checks':checks,'index_parity':{'urls_enumerated_from_valid_indexes':len(urls),'source_records':len(records),'parity':len(urls)==len(records),'enumeration_sources':[x['url'] for x in checks if x['valid_document_index']]},'sources':records,'coverage_statement':f'Full valid-index coverage: {len(records)} of {len(urls)} deduplicated URLs enumerated from official valid indexes have an explicit source record; successful/recovered={success}; unavailable={unavailable}. Corpus is '+('fully collected.' if unavailable==0 else 'not fully compliant because unavailable sources persist.')}
    (root/'source-manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    (root/'COVERAGE.md').write_text('# Full-index coverage\n\n'+manifest['coverage_statement']+'\n')
    print(product, 'enumerated',len(urls),'successful',success,'unavailable',unavailable,'indexes',[(x['kind'],x['status'],x['valid_document_index'],x['enumerated_url_count']) for x in checks])
if __name__=='__main__': main()
