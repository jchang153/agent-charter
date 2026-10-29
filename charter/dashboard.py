"""Read-only local observer: serves only projected run state, never host files."""
from __future__ import annotations

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from zoneinfo import ZoneInfo


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def observer_snapshot(run_dir):
    root = Path(run_dir)
    manifest = read_json(root / "manifest.json", {})
    shared = read_json(root / "shared/snapshot.json", {})
    billing = read_json(root / "billing_summary.json", read_json(root / "billing/accounting.json", {}))
    actors = manifest.get("config", {}).get("actors", [])
    participants = []
    for actor in actors:
        identity = actor["agent_id"]
        state = shared.get("participants", {}).get(identity, {})
        responses = read_json(root / identity / "model_responses.json", [])
        result = read_json(root / identity / "result.json", {})
        participants.append({"id": identity, "model": actor["model"], "status": state.get("status", "starting"),
            "signed": bool(state.get("signature")), "signature": state.get("signature"), "responses": len(responses),
            "stop_reason": result.get("stop_reason"), "reason": state.get("reason"),
            "objections": state.get("unresolved_objections"),
            "budget_withdrawal": identity in shared.get("controller_withdrawals", {}),
            "technical_exclusion": shared.get("controller_exclusions", {}).get(identity)})
    return {"updated_at": datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(),
        "run_status": manifest.get("status", "starting"), "outcome": shared.get("outcome", "incomplete"),
        "constitution": shared.get("constitution", {"revision": 0, "text": ""}),
        "participants": participants, "messages": shared.get("messages", []),
        "history": shared.get("history", []), "approval_history": shared.get("approval_history", []),
        "required_signers": shared.get("required_signers", [p["id"] for p in participants
            if p["status"] not in {"withdrawn", "technical_excluded"}]),
        "opening_review": shared.get("opening_review", {}),
        "experiment_version": manifest.get("experiment_version", 2),
        "collaboration": shared.get("collaboration", {}), "billing": {
            "spent_usd": billing.get("spent_usd", billing.get("spent", 0)),
            "accounting_uncertain": billing.get("accounting_uncertain", False)}}


def render(run_dir):
    template = Path(__file__).with_name("dashboard.html").read_text()
    payload = json.dumps(observer_snapshot(run_dir), ensure_ascii=False).replace("<", "\\u003c")
    return template.replace("/* INITIAL_STATE */ null", payload)


def start_dashboard(run_dir):
    root = Path(run_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/":
                raw, mime = render(root).encode(), "text/html; charset=utf-8"
            elif self.path.split("?", 1)[0] == "/snapshot":
                raw, mime = json.dumps(observer_snapshot(root), ensure_ascii=False).encode(), "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, f"http://127.0.0.1:{server.server_port}/"


def export_dashboard(run_dir):
    root = Path(run_dir)
    temporary = root / "dashboard.html.tmp"
    temporary.write_text(render(root))
    temporary.replace(root / "dashboard.html")
