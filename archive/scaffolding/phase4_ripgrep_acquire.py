import hashlib, json
from pathlib import Path
r=Path('Doc Corpus/reports/runs/connected-rehearsal-ripgrep-phase4')
s=r/'sources'
fs=sorted(p for p in s.rglob('*') if p.is_file())
rows=[{'path':str(p.relative_to(Path('Doc Corpus'))),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'canonical_url':'https://raw.githubusercontent.com/BurntSushi/ripgrep/14.1.0/'+str(p.relative_to(s/'ripgrep/14.1.0'))} for p in fs]
m={'run_id':'connected-rehearsal-ripgrep-phase4','caller':'default','bank':'Liv','product':'ripgrep','version':'14.1.0','source_id':'ripgrep_14_1_0_github_tag','corpus_root':str(Path('Doc Corpus').resolve()),'documentation_roots':['reports/runs/connected-rehearsal-ripgrep-phase4/sources/ripgrep/14.1.0'],'files':rows}
(r/'manifest.json').write_text(json.dumps(m,indent=2)+'\n')
(r/'gaps.json').write_text(json.dumps({'run_id':m['run_id'],'status':'partial','gaps':['No first-party llms.txt or sitemap was found for the GitHub repository.','External Rust dependency documentation was not recursively acquired.','Generated man page and shell completions were not separately acquired.'],'exclusions':['Release binaries and checksums are assets rather than authored documentation.']},indent=2)+'\n')
(r/'agent-note.md').write_text('Acquired first-party authored documentation and metadata from the official ripgrep 14.1.0 Git tag. Coverage is partial; see gaps.json. No Hindsight ingestion or submission was performed.\n')
