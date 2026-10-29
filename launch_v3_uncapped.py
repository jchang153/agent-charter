"""Explicitly authorized continuation of Run 3 with no dollar admission cap."""
import json,os,sys,threading,uuid,signal
from pathlib import Path
from datetime import datetime,timezone
project=Path(__file__).resolve().parent
sys.path.insert(0,str(project))
from charter.runner import live,save,validate_config
from charter.dashboard import start_dashboard,export_dashboard
from charter import secrets
signal.signal(signal.SIGINT,signal.default_int_handler)
source=Path(sys.argv[1]).resolve()
prior=json.loads((source/'manifest.json').read_text())
raw=json.loads((project/'config.v3.json').read_text());raw['agent_budget_usd']=None
config=validate_config(raw)
catalog=json.loads((source/'model_catalog.json').read_text())
run=project/'runs'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_seminar_v3_uncapped_'+uuid.uuid4().hex[:8])
server,url=start_dashboard(run)
save(project/'CURRENT_RUN.json',{'run_dir':str(run),'dashboard_url':url,'pid':os.getpid(),'authorized_agent_count':len(config['actors']),'per_agent_allowance_usd':None,'monitor_automation':'monitor-constitution-seminar','experiment_version':3,'fresh_run':False,'resume_from':str(source),'budget_change_authorized':True})
print(json.dumps({'event':'uncapped_continuation_launching','run_dir':str(run),'dashboard_url':url}),flush=True)
finished=threading.Event()
def observer():
 while not finished.wait(15):
  if (run/'manifest.json').exists():export_dashboard(run)
threading.Thread(target=observer,daemon=True).start()
secrets.ALLOWED_KEYS=secrets.ALLOWED_KEYS|{'ANTHROPIC_API_KEY'}
def credentials():
 parsed=secrets.read_credentials(Path('/Users/jchang153/Documents/GitHub/healing-agent-swarms/.env'))
 return {'OPENROUTER_API_KEY':parsed.get('OPENROUTER_API_KEY')}
try:
 live(config,run,catalog,docker='/Users/jchang153/.docker/bin/docker',credential_reader=credentials,resume_from=source,authorized_budget_change=True)
 print(json.dumps({'event':'continuation_finished','run_dir':str(run)}),flush=True)
except BaseException as error:
 print(json.dumps({'event':'continuation_interrupted','error_type':type(error).__name__,'run_dir':str(run)}),flush=True)
finally:
 finished.set()
 if (run/'manifest.json').exists():export_dashboard(run)
try:threading.Event().wait()
except KeyboardInterrupt:server.shutdown()
