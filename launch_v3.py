"""Authorized fresh shared-authorship run and read-only observer."""
from pathlib import Path
from datetime import datetime, timezone
import json
import os
import sys
import threading
import uuid

project = Path('/Users/jchang153/Documents/GitHub/agent-constitution')
sys.path.insert(0, str(project))
from charter.runner import live, save, validate_config
from charter.dashboard import start_dashboard, export_dashboard
from charter import secrets

config = validate_config(json.loads((project / 'config.v3.json').read_text()))
catalog = json.loads((project / 'model-catalog.v3.json').read_text())
run = project / 'runs' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_seminar_v3_' + uuid.uuid4().hex[:8])
server, url = start_dashboard(run)
save(project / 'CURRENT_RUN.json', {'run_dir': str(run), 'dashboard_url': url, 'pid': os.getpid(),
    'authorized_agent_count': len(config['actors']), 'per_agent_allowance_usd': 10,
    'monitor_automation': 'monitor-constitution-seminar', 'experiment_version': 3, 'fresh_run': True})
print(json.dumps({'event': 'launching', 'run_dir': str(run), 'dashboard_url': url}), flush=True)
finished = threading.Event()

def save_launch_profile():
    save(run / 'launch_profile.json', {'fresh_run': True, 'experiment_version': 3,
        'original_population': len(config['actors']), 'per_agent_allowance_usd': 10,
        'prior_experiments_retained': True, 'interpreter': sys.executable})
    (run / 'launch_v3.py').write_text(Path(__file__).read_text())

def observer_export():
    while not finished.wait(15):
        if (run / 'manifest.json').exists():
            save_launch_profile()
            export_dashboard(run)
threading.Thread(target=observer_export, daemon=True).start()

# Only the authorized credential reader sees the source file. Ignore unused
# historical entries; return only the OpenRouter credential to the host gateway.
secrets.ALLOWED_KEYS = secrets.ALLOWED_KEYS | {'ANTHROPIC_API_KEY'}
def openrouter_credentials():
    parsed = secrets.read_credentials(Path('/Users/jchang153/Documents/GitHub/healing-agent-swarms/.env'))
    return {'OPENROUTER_API_KEY': parsed.get('OPENROUTER_API_KEY')}

try:
    live(config, run, catalog, docker='/Users/jchang153/.docker/bin/docker', credential_reader=openrouter_credentials)
    print(json.dumps({'event': 'experiment_finished', 'run_dir': str(run)}), flush=True)
except BaseException as error:
    print(json.dumps({'event': 'experiment_interrupted', 'error_type': type(error).__name__, 'run_dir': str(run)}), flush=True)
finally:
    finished.set()
    if (run / 'manifest.json').exists():
        save_launch_profile()
        export_dashboard(run)
# Preserve the local observer for manual reading after completion.
try:
    threading.Event().wait()
except KeyboardInterrupt:
    server.shutdown()
