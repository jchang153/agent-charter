"""Exact task-prefix continuation after diagnosed controller transport stops."""
import json
import math
from pathlib import Path
import shutil
from .prompts import initial_messages


def continuation_plan(source, config):
    root = Path(source).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    billing = json.loads((root / "billing_summary.json").read_text())
    shared = json.loads((root / "shared/result.json").read_text())
    accounting = json.loads((root / "billing/accounting.json").read_text())
    if (manifest.get("status") != "finished" or not manifest.get("archive_complete")
            or manifest["config"]["actors"] != config["actors"] or shared["status"] != "active"):
        raise ValueError("continuation requires an archived incomplete segment with the same population")
    if accounting.get("accounting_uncertain") and not accounting.get("recoverable_uncertainty"):
        raise ValueError("current segment has nonrecoverable accounting uncertainty")
    events = [json.loads(line) for line in (root / "shared/events.jsonl").read_text().splitlines()]
    limits = {}
    for actor in config["actors"]:
        identity = actor["agent_id"]
        result = json.loads((root / identity / "result.json").read_text())
        error = json.loads((root / identity / "error.json").read_text())
        if (shared["participants"][identity]["status"] != "interrupted"
                or result["stop_reason"] != "generation_or_tool_error" or error["error_type"] != "HTTPError"):
            raise ValueError("continuation may restore only diagnosed HTTP transport interruptions, never voluntary exits")
        if not (root / "private_workspaces" / identity).is_dir():
            raise ValueError("missing private workspace export")
        if not any(event["action"] == "interrupt" and event["detail"].get("participant") == identity
                   and event["detail"].get("reason") == "generation_or_tool_error" for event in events):
            raise ValueError("missing controller interruption provenance")
        transcript = json.loads((root / identity / "transcript.json").read_text())
        if transcript[:2] != initial_messages(shared_authorship=config.get("shared_authorship", False), draft_workspace=config.get("draft_workspace", False), discussion_max_chars=config.get("discussion_max_chars", 64000), web_research=config.get("web_research", False)):
            raise ValueError("continuation must preserve exact prompts")
        pending = set()
        for message in transcript:
            if message["role"] == "assistant":
                if pending:
                    raise ValueError("ambiguous incomplete tool effects")
                calls = message.get("tool_calls", [])
                pending = {call["id"] for call in calls}
                if len(pending) != len(calls):
                    raise ValueError("duplicate tool calls")
            elif message["role"] == "tool":
                if message["tool_call_id"] not in pending:
                    raise ValueError("tool result without unique pending call")
                pending.remove(message["tool_call_id"])
        if pending:
            raise ValueError("cannot replay pending tool effects")
        costs = billing["agents"][identity]
        remaining = costs["limit_usd"] - costs["spent_usd"] - costs["reserved_usd"]
        if not math.isfinite(remaining) or not 0 < remaining <= 10:
            raise ValueError("invalid remaining cumulative allowance")
        limits[identity] = remaining
    return root, billing, limits


def restore_shared(source, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    # The journal contains all state mutations; derived exports are regenerated.
    shutil.copyfile(Path(source) / "shared/events.jsonl", destination / "events.jsonl")


def budget_change_plan(source, config):
    """Validate an authorized admission-policy continuation; never restore voluntary exits."""
    root=Path(source).resolve()
    manifest=json.loads((root/'manifest.json').read_text())
    billing=json.loads((root/'billing_summary.json').read_text())
    shared=json.loads((root/'shared/result.json').read_text())
    if (config.get('agent_budget_usd') is not None and not config.get('recorded_spending_only')) or manifest.get('status') not in {'finished','interrupted'} or not manifest.get('archive_complete') or shared['status']!='active' or manifest['config']['actors']!=config['actors']:
        raise ValueError('requires an archived active session with the same roster and explicit changed admission policy')
    if billing.get('accounting_uncertain') and not billing.get('safe_to_continue'):raise ValueError('unsafe accounting requires reconciliation')
    events=list(map(json.loads,(root/'shared/events.jsonl').read_text().splitlines()))
    for actor in config['actors']:
        identity=actor['agent_id'];p=shared['participants'][identity]
        budget_exit=shared.get('controller_withdrawals',{}).get(identity,{}).get('cause')=='budget_exhausted'
        if not ((p['status']=='withdrawn' and budget_exit) or (p['status']=='interrupted' and any(e.get('action')=='interrupt' and e.get('detail',{}).get('participant')==identity and e.get('detail',{}).get('reason')=='controller_stopped' for e in events))):raise ValueError('cannot revive voluntary withdrawal or technical exclusion')
        if not (root/'private_workspaces'/identity).is_dir():raise ValueError('missing workspace archive')
        transcript=json.loads((root/identity/'transcript.json').read_text())
        if transcript[:2]!=initial_messages(shared_authorship=config.get('shared_authorship',False), draft_workspace=config.get('draft_workspace',False), discussion_max_chars=config.get('discussion_max_chars',64000), web_research=config.get('web_research',False)):raise ValueError('prompt mismatch')
        pending=set()
        for message in transcript:
            if message['role']=='assistant':
                if pending:raise ValueError('unfinished tool effects')
                pending={call['id'] for call in message.get('tool_calls',[])}
            elif message['role']=='tool':
                if message['tool_call_id'] not in pending:raise ValueError('ambiguous tool result')
                pending.remove(message['tool_call_id'])
        # Final pending calls were not executed after the controlled pause.
        # The continuation records explicit cancellation results instead of replaying them.
    limits = {actor['agent_id']: (None if config.get('agent_budget_usd') is None else config['agent_budget_usd'] - billing['agents'][actor['agent_id']].get('cumulative_spent_usd', billing['agents'][actor['agent_id']]['spent_usd'])) for actor in config['actors']}
    if any(v is not None and v <= 0 for v in limits.values()):raise ValueError('recorded spending already reached allowance')
    return root,billing,limits


def policy_change_transcript(transcript):
    import copy
    result=copy.deepcopy(transcript);pending={}
    for message in result:
        if message['role']=='assistant':
            if pending:raise ValueError('ambiguous incomplete tool effects')
            pending={call['id']:call for call in message.get('tool_calls',[])}
        elif message['role']=='tool':
            if message['tool_call_id'] not in pending:raise ValueError('unexpected tool result')
            pending.pop(message['tool_call_id'])
    canceled=list(pending)
    for identity in canceled:
        result.append({'role':'tool','tool_call_id':identity,'content':json.dumps({'ok':False,'executed':False,'error':'Action canceled by a controller pause. Read the current session state before choosing another action.'})})
    result.append({'role':'user','content':'The controller paused and resumed this same discussion. Previously paused participants are active again. Previously pending actions, if any, were canceled rather than replayed. Read session_status and the current discussion before continuing.'})
    return result,canceled
