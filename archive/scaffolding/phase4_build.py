import tarfile, pathlib, hashlib, json
archive=pathlib.Path('/home/jsondarula/Liv-Home/Doc Corpus/reports/runs/acquire-only-real-20260908T002500Z/sources/ripgrep/14.1.0/ripgrep-14.1.0.tar.gz')
corpus=pathlib.Path('/home/jsondarula/Liv-Home/Doc Corpus'); run=corpus/'rehearsal/ripgrep-normal-rehearsal-phase4-20260908T002500Z'; out=run/'sources/first-party/ripgrep/14.1.0/extracted'; out.mkdir(parents=True,exist_ok=True)
with tarfile.open(archive,'r:gz') as t:
    for m in t.getmembers():
        p=pathlib.PurePosixPath(m.name)
        if p.is_absolute() or '..' in p.parts: raise SystemExit('unsafe '+m.name)
    safe=[m for m in t.getmembers() if not (m.issym() or m.islnk())]
    t.extractall(out, members=safe)
root=out/'ripgrep-14.1.0'; selected=[]
for p in root.rglob('*'):
    if not p.is_file(): continue
    rel=p.relative_to(root).as_posix(); n=p.name
    if n in {'README.md','README','README.SETUP','FAQ.md','GUIDE.md','CHANGELOG.md','RELEASE-CHECKLIST.md','COPYING','LICENSE-MIT','UNLICENSE'} or rel in {'doc/rg.1.txt.tpl','crates/core/flags/complete/encodings.sh'} or (rel.startswith('.github/ISSUE_TEMPLATE/') and n in {'feature_request.md','bug_report.yml'}): selected.append((rel,p))
files=[]
for rel,p in sorted(selected):
    data=p.read_bytes(); ext=p.suffix.lower(); mime={'.md':'text/markdown','.txt':'text/plain','.yml':'text/yaml','.sh':'text/plain','.tpl':'text/plain'}.get(ext,'text/plain')
    files.append({'path':p.relative_to(corpus).as_posix(),'sha256':hashlib.sha256(data).hexdigest(),'canonical_url':f'https://github.com/BurntSushi/ripgrep/blob/14.1.0/{rel}','mime_type':mime})
manifest={'run_id':'ripgrep-normal-rehearsal-phase4-20260908T002500Z','caller':'default','bank':'Liv','product':'ripgrep','version':'14.1.0','source_id':'ripgrep_14_1_0_docs_phase4','documentation_roots':['rehearsal/ripgrep-normal-rehearsal-phase4-20260908T002500Z/sources/first-party/ripgrep/14.1.0/extracted'],'files':files}
(run/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n'); (run/'gaps.json').write_text(json.dumps({'run_id':manifest['run_id'],'boundary':'first-party repository documentation in the 14.1.0 source archive','residual_gaps':[{'path':'security/advisories','reason':'No dedicated security/advisory document present.'},{'path':'release assets','reason':'Binary release assets were not present or required; archive preserved as provenance.'},{'path':'source and tests','reason':'Implementation, tests, fixtures, CI, build metadata, benchmark data, and non-documentation scripts excluded.'},{'path':'generated sources','reason':'Only explicitly documentation-oriented man/completion sources included.'}]},indent=2)+'\n')
(run/'agent-note.md').write_text(f'Phase 4 local rehearsal evidence\n\n- Product/version: ripgrep 14.1.0.\n- Archive provenance: {archive}; first-party BurntSushi/ripgrep tag 14.1.0. Original archive preserved.\n- Safely extracted regular members under {out}; no code was executed, built, tested, sourced, or run.\n- Manifest contains {len(files)} authored documentation records, each SHA-256 hashed from exact extracted bytes.\n- Excluded implementation/source and test files, fixtures/data, CI/workflow/build/package metadata, benchmark data, and non-documentation scripts. No dedicated security/advisory document was present. See gaps.json.\n- No production ingestion, submission, POST, network access, or Hindsight modification performed.\n')
print(len(files))
