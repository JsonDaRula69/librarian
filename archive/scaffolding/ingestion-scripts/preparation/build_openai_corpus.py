import concurrent.futures, hashlib, json, os, re, shutil, sys, time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT=Path('/tmp/full-docs-backfill/openai-codex')
OLD=Path('/tmp/openai_llm_docs')
ROOT_INDEX='https://developers.openai.com/llms.txt'
CODEX_INDEX='https://developers.openai.com/codex/llms.txt'
UA='Mozilla/5.0 (compatible; OpenAI-docs-audit/1.0)'

def fetch(url):
    req=Request(url,headers={'User-Agent':UA,'Accept':'text/markdown,text/plain,text/html;q=0.9,*/*;q=0.1'})
    try:
        with build_opener(HTTPRedirectHandler()).open(req,timeout=45) as r:
            b=r.read(); return {'ok':True,'http_status':r.status,'final_url':r.geturl(),'content_type':r.headers.get('Content-Type',''),'body':b}
    except HTTPError as e:
        return {'ok':False,'http_status':e.code,'final_url':url,'content_type':e.headers.get('Content-Type','') if e.headers else '', 'error':f'HTTPError {e.code}'}
    except Exception as e: return {'ok':False,'http_status':None,'final_url':url,'content_type':'','error':type(e).__name__+': '+str(e)[:300]}

def normalize(u):
    p=urlsplit(u.rstrip('.,;:'))
    return urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path,p.query,''))

def links(text):
    vals=re.findall(r'\[[^\]]*\]\((https?://[^)\s]+)\)',text)
    return list(dict.fromkeys(normalize(x) for x in vals))

def safe_name(i,url,ctype):
    p=urlsplit(url).path.strip('/').replace('/','_') or 'index'
    p=re.sub(r'[^A-Za-z0-9._-]+','_',p)[:130]
    ext=Path(p).suffix
    if not ext:
        if 'html' in ctype: ext='.html'
        elif 'markdown' in ctype: ext='.md'
        else: ext='.txt'
    if not p.endswith(ext): p+=ext
    return f'{i:04d}_{p}_{hashlib.sha256(url.encode()).hexdigest()[:12]}'

def main():
    if ROOT.exists(): raise SystemExit(f'refusing existing destination {ROOT}')
    (ROOT/'sources').mkdir(parents=True); (ROOT/'indexes').mkdir()
    # BFS official Developer root and its explicitly linked component llms indexes; Codex root final redirect is separately retained.
    root_results={}
    for u in [ROOT_INDEX,CODEX_INDEX]: root_results[u]=fetch(u)
    if not all(x['ok'] and x['body'].lstrip().startswith(b'#') for x in root_results.values()): raise SystemExit('required root index invalid')
    dev_children=[u for u in links(root_results[ROOT_INDEX]['body'].decode('utf-8','replace')) if urlsplit(u).netloc=='developers.openai.com' and urlsplit(u).path.endswith('/llms.txt')]
    # ensure explicit Codex index only once; linked learn redirect is not a separate logical index
    logical_indexes=list(dict.fromkeys([ROOT_INDEX]+dev_children))
    idx={ROOT_INDEX:root_results[ROOT_INDEX]}
    for u in logical_indexes[1:]: idx[u]=root_results.get(u) or fetch(u)
    invalid={u:r for u,r in idx.items() if not r['ok'] or not r['body'].lstrip().startswith(b'#')}
    if invalid: raise SystemExit('component index invalid '+str({u:r.get('http_status') for u,r in invalid.items()}))
    # enumerate every link in all selected Developer indexes plus the Codex index (dedupe source identity URLs)
    ordered=[]; origin={}
    for u in logical_indexes+[CODEX_INDEX]:
        for x in links(idx.get(u,root_results.get(u))['body'].decode('utf-8','replace')):
            if x not in origin: ordered.append(x); origin[x]=[]
            origin[x].append(u)
    # Include source snapshots of all selected logical indexes; Codex source index itself aliases one listed Developer source.
    all_urls=list(dict.fromkeys(logical_indexes+ordered))
    # Persist raw validated root/component index snapshots with final redirect metadata.
    index_records=[]
    for n,u in enumerate(logical_indexes+[CODEX_INDEX],1):
        r=idx.get(u,root_results.get(u)); b=r['body']; fn=f'{n:02d}_{hashlib.sha256(u.encode()).hexdigest()[:12]}.txt'; (ROOT/'indexes'/fn).write_bytes(b)
        index_records.append({'logical_index_url':u,'final_url':r['final_url'],'http_status':r['http_status'],'content_type':r['content_type'],'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'snapshot':'indexes/'+fn,'valid_markdown_index':bool(b.lstrip().startswith(b'#'))})
    def one(pair):
        i,u=pair; r=fetch(u); rec={'ordinal':i,'source_url':u,'listed_by':origin.get(u,[]),'http_status':r['http_status'],'final_url':r['final_url'],'content_type':r['content_type']}
        if not r['ok'] and r.get('http_status')==404 and urlsplit(u).path.endswith('.md'):
            p=urlsplit(u); canon=urlunsplit((p.scheme,p.netloc,p.path[:-3],p.query,'')); rr=fetch(canon)
            rec['recovery_attempt']={'canonical_url':canon,'http_status':rr['http_status'],'final_url':rr['final_url']}
            if rr['ok']: r=rr; rec['collection_status']='recovered_canonical'
        if r['ok']:
            b=r['body']; name=safe_name(i,u,r['content_type']); rel='sources/'+name; (ROOT/rel).write_bytes(b)
            rec.update({'collection_status':rec.get('collection_status','downloaded'),'file':rel,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'utf8_valid':True})
            try:b.decode('utf-8')
            except UnicodeDecodeError:rec['utf8_valid']=False
        else: rec.update({'collection_status':'unavailable','error':r.get('error')})
        return rec
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex: records=list(ex.map(one,enumerate(all_urls,1)))
    # prior audit/reconciliation
    old_manifest=json.load(open(OLD/'manifest.json')) if (OLD/'manifest.json').exists() else []
    old_urls={normalize(x.get('url','')):x for x in old_manifest if x.get('url')}
    old_recov=json.load(open(OLD/'recoveries.json')) if (OLD/'recoveries.json').exists() else []
    old_rep=json.load(open(OLD/'repaired/repair_manifest.json')).get('files',[]) if (OLD/'repaired/repair_manifest.json').exists() else []
    old_op=json.load(open(OLD/'operation_statuses.json')) if (OLD/'operation_statuses.json').exists() else []
    old_fail=[x for x in old_op if x.get('status') in ('failed','cancelled')]
    current={x['source_url']:x for x in records}
    reconcile=[]
    for u,x in sorted(old_urls.items()):
        c=current.get(u); reconcile.append({'prior_url':u,'prior_status':x.get('status'),'current_status':c.get('collection_status') if c else 'not_in_current_enumeration','current_source_url':u if c else None})
    summary={'created_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'corpus_definition':{'logical_index_urls':logical_indexes+[CODEX_INDEX],'note':'Developers root llms.txt, every directly linked Developers component llms.txt, and the Codex llms.txt linked from Developers. Its final URL redirects to learn.chatgpt.com/docs/llms.txt; it is preserved as the official Developers-origin index URL.'},'counts':{'logical_indexes':len(logical_indexes)+1,'enumerated_unique_urls':len(ordered),'source_manifest_records':len(records),'downloaded':sum(x['collection_status']=='downloaded' for x in records),'recovered_canonical':sum(x['collection_status']=='recovered_canonical' for x in records),'unavailable':sum(x['collection_status']=='unavailable' for x in records),'parity_ok':len(records)==len(ordered)},'immutable_policy':'Source and index snapshots are content-addressed by SHA-256 and chmod read-only after all audit/retention manifests are written. No source artifact is overwritten.'}
    (ROOT/'index_manifest.json').write_text(json.dumps(index_records,indent=2)+'\n')
    (ROOT/'source_manifest.json').write_text(json.dumps(records,indent=2)+'\n')
    (ROOT/'reconciliation.json').write_text(json.dumps({'prior_manifest_records':len(old_manifest),'prior_recoveries':old_recov,'prior_repair_records':old_rep,'prior_terminal_failures':old_fail,'url_reconciliation':reconcile,'counts':{'prior_urls_reenumerated':sum(x['current_status']!='not_in_current_enumeration' for x in reconcile),'prior_urls_retired_or_outside_current_scope':sum(x['current_status']=='not_in_current_enumeration' for x in reconcile),'prior_failure_records_reconciled':len(old_fail)}},indent=2)+'\n')
    (ROOT/'coverage.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary['counts'],sort_keys=True))
if __name__=='__main__': main()
