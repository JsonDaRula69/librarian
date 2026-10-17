import json
from pathlib import Path
from broker.librarian_completion import validate_run
r=Path('Doc Corpus/reports/runs/broadcom-linux-full-corpus-eval-v3-20260907')
c=json.loads((r/'completion-contract.json').read_text())
v=validate_run(r,c)
(r/'completion-validation.json').write_text(json.dumps(v,indent=2)+'\n')
print(json.dumps(v))
