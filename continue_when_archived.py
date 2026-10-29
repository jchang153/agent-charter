"""Wait for the controlled pause archive, then continue this authorized run once."""
import json,os,sys,time
from pathlib import Path
from charter.runner import validate_config
from charter.continuation import budget_change_plan
root=Path(__file__).resolve().parent
source=Path(sys.argv[1]).resolve()
while True:
 manifest=json.loads((source/'manifest.json').read_text())
 if manifest.get('status') in {'finished','interrupted'} and manifest.get('archive_complete'):break
 time.sleep(5)
raw=json.loads((root/'config.v3.json').read_text());raw['agent_budget_usd']=None
budget_change_plan(source,validate_config(raw))
print(json.dumps({'event':'archive_validated_for_authorized_continuation','source':str(source)}),flush=True)
os.execv(sys.executable,[sys.executable,'-u',str(root/'launch_v3_uncapped.py'),str(source)])
