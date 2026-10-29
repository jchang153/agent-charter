"""Launch the user-authorized ten-model seminar with a local observer."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import json
import os
import threading
import uuid

from charter.runner import configure_prices, live, save, validate_config
from charter.dashboard import start_dashboard, export_dashboard
from charter.secrets import read_credentials


def main():
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', type=Path, default=project / '.env')
    parser.add_argument('--docker', default='/Users/jchang153/.docker/bin/docker')
    args = parser.parse_args()
    config = validate_config(json.loads((project / 'config.run4.json').read_text()))
    catalog = json.loads((project / 'model-catalog.run4.json').read_text())
    configure_prices(catalog, config['actors'])
    credentials = read_credentials(args.credentials, ignore_unknown=True)
    if not credentials.get('OPENROUTER_API_KEY', '').strip():
        raise RuntimeError('Experiment OpenRouter credential unavailable')
    credentials = {'OPENROUTER_API_KEY': credentials['OPENROUTER_API_KEY']}
    run = project / 'runs' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                            + '_seminar_run4_' + uuid.uuid4().hex[:8])
    server, url = start_dashboard(run)
    finished = threading.Event()

    def observer_export():
        while not finished.wait(15):
            if (run / 'manifest.json').exists():
                export_dashboard(run)

    threading.Thread(target=observer_export, daemon=True).start()
    try:
        save(project / 'CURRENT_RUN.json', {
            'run_dir': str(run), 'dashboard_url': url, 'pid': os.getpid(),
            'authorized_agent_count': len(config['actors']),
            'per_agent_allowance_usd': config['agent_budget_usd'],
            'experiment_version': 3, 'experiment_number': 4, 'fresh_run': True,
        })
        print(json.dumps({'event': 'launching', 'run_dir': str(run),
                          'dashboard_url': url}), flush=True)
        live(config, run, catalog, docker=args.docker,
             credential_reader=lambda: credentials)
        print(json.dumps({'event': 'experiment_finished', 'run_dir': str(run)}), flush=True)
    except BaseException as error:
        print(json.dumps({'event': 'experiment_interrupted',
                          'error_type': type(error).__name__, 'run_dir': str(run)}), flush=True)
        raise
    finally:
        credentials = None
        finished.set()
        if (run / 'manifest.json').exists():
            save(run / 'launch_profile.json', {
                'experiment_number': 4, 'experiment_version': 3,
                'fresh_run': True, 'original_population': len(config['actors']),
                'per_agent_allowance_usd': config['agent_budget_usd'],
                'prior_experiments_retained': True,
            })
            (run / 'launch_run4.py').write_text(Path(__file__).read_text())
            export_dashboard(run)
    # Keep the read-only observer available after the experiment finishes.
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == '__main__':
    main()
