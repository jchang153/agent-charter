"""Prepare offline or explicitly launch a budget-bounded constitution seminar.

No turns/time limit. Model populations must be chosen explicitly. Imports,
configuration validation, and --prepare never launch containers or model calls.
"""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
import urllib.request
import urllib.error
import uuid

from .prompts import initial_messages
from .runtime import DockerRuntime, validate_population
from .state import SessionState
from .tools import tool_definitions

ROOT = Path(__file__).resolve().parents[1]
AGENT_BUDGET_USD = 10.0
FORMAT_RECOVERY_RETRIES = 2
FORMAT_REMINDER = (
    "Continue participating in the shared task through the available tools. "
    "Return a valid assistant tool-call response with JSON object arguments. "
    "Use session_status if you need the current state."
)


class MalformedModelResponse(ValueError):
    """Provider response cannot be executed or replayed as a tool message."""


def validated_assistant(response):
    try:
        choice = response["choices"][0]
        assistant = choice["message"]
        if choice.get("finish_reason") == "error":
            raise MalformedModelResponse("provider_finish_error")
        if not isinstance(assistant, dict) or assistant.get("role") != "assistant":
            raise MalformedModelResponse("missing_assistant")
        calls = assistant.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            raise MalformedModelResponse("truncated_without_tool_calls" if choice.get("finish_reason") == "length"
                                         else "missing_tool_calls")
        ids = set()
        for call in calls:
            if (not isinstance(call, dict) or call.get("type") != "function"
                    or not isinstance(call.get("id"), str) or not call["id"] or call["id"] in ids):
                raise MalformedModelResponse("invalid_tool_call")
            ids.add(call["id"])
            function = call.get("function")
            if (not isinstance(function, dict) or not isinstance(function.get("name"), str)
                    or not function["name"] or not isinstance(function.get("arguments"), str)
                    or not isinstance(json.loads(function["arguments"]), dict)):
                raise MalformedModelResponse("invalid_tool_arguments")
        return assistant
    except (KeyError, IndexError, TypeError, ValueError) as error:
        if isinstance(error, MalformedModelResponse):
            raise
        raise MalformedModelResponse("invalid_response_structure") from None


def audit_intervention(outdir, event, **fields):
    path = Path(outdir) / "interventions.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "time": datetime.now(timezone.utc).isoformat(), **fields}) + "\n")


def interrupt_unfinished(state, actors):
    """Cancellation wakes unresolved workers; finalized signatures are immutable."""
    if state.snapshot()["status"] == "active":
        for actor in actors:
            state.interrupt(actor["agent_id"], "controller_stopped")


class BudgetExhausted(Exception):
    """Local admission exhaustion, never a provider authentication/API error."""


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("configuration must be an object")
    allowed = {"population", "agent_budget_usd", "max_output_tokens", "max_in_flight", "temperature", "shared_authorship", "draft_workspace", "discussion_max_chars", "web_research", "recorded_spending_only"}
    if set(config) - allowed:
        raise ValueError("unknown configuration fields; turn/time limits are not supported")
    if type(config.get("recorded_spending_only", False)) is not bool:
        raise ValueError("recorded_spending_only must be boolean")
    if type(config.get("shared_authorship", False)) is not bool:
        raise ValueError("shared_authorship must be boolean")
    if type(config.get("draft_workspace", False)) is not bool:
        raise ValueError("draft_workspace must be boolean")
    if type(config.get("web_research", False)) is not bool:
        raise ValueError("web_research must be boolean")
    discussion_limit = config.get("discussion_max_chars", 64000)
    if type(discussion_limit) is not int or not 1 <= discussion_limit <= 64000:
        raise ValueError("discussion_max_chars must be between 1 and 64000")
    actors = validate_population(config.get("population"))
    if len(actors) > 100:
        raise ValueError("at most 100 participants are supported")
    allowance = config.get("agent_budget_usd", AGENT_BUDGET_USD)
    if allowance is not None and (type(allowance) not in (int, float)
                                 or not math.isfinite(allowance) or allowance <= 0):
        raise ValueError("agent_budget_usd must be a positive finite allowance or null")
    tokens = config.get("max_output_tokens", 8192)
    concurrency = config.get("max_in_flight", len(actors))
    temperature = config.get("temperature", 0.7)
    if type(tokens) is not int or not 1 <= tokens <= 65536:
        raise ValueError("max_output_tokens must be an integer between 1 and 65536")
    if type(concurrency) is not int or not 1 <= concurrency <= 100:
        raise ValueError("max_in_flight must be an integer between 1 and 100")
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError("temperature must be finite and between 0 and 2")
    return {"population": config["population"], "actors": actors,
            "agent_budget_usd": config.get("agent_budget_usd", AGENT_BUDGET_USD), "max_output_tokens": tokens,
            "max_in_flight": concurrency, "temperature": temperature,
            "shared_authorship": config.get("shared_authorship", False),
            "draft_workspace": config.get("draft_workspace", False),
            "discussion_max_chars": discussion_limit,
            "web_research": config.get("web_research", False),
            "recorded_spending_only": config.get("recorded_spending_only", False)}


def configure_prices(catalog, actors):
    from . import budget
    entries = {item["id"]: item for item in catalog["data"]}
    record = {}
    for model in sorted({actor["model"] for actor in actors}):
        if model not in entries:
            raise ValueError("selected model is missing from the saved provider catalog")
        item = entries[model]
        if "tools" not in item.get("supported_parameters", []):
            raise ValueError("selected model does not advertise tool support")
        price_tiers = [item["pricing"], *item["pricing"].get("overrides", [])]
        rates = tuple(max(float(tier.get(key, item["pricing"][key])) for tier in price_tiers) * 1_000_000
                      for key in ("prompt", "completion"))
        if any(not math.isfinite(rate) or rate < 0 for rate in rates):
            raise ValueError("invalid catalog pricing")
        premium = max(float(tier.get("input_cache_write", 0)) for tier in price_tiers) * 1_000_000
        if not math.isfinite(premium) or premium < 0:
            raise ValueError("invalid catalog cache pricing")
        reserved = (max(rates[0], premium) * 2, rates[1] * 2)
        budget.MODEL_PRICES[model] = reserved
        record[model] = {"max_published_tier_usd_per_million": rates, "reservation_usd_per_million": reserved,
                         "supported_parameters": item.get("supported_parameters", []),
                         "reasoning_efforts": item.get("reasoning", {}).get("supported_efforts", [])}
    return record


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_json(base_url, body, actor):
    request = urllib.request.Request(base_url + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-agent-id": actor})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=630) as response:
        raw = response.read(5 * 1024**2 + 1)
    if len(raw) > 5 * 1024**2:
        raise ValueError("provider response exceeds size limit")
    return json.loads(raw)


def run_actor(actor, state, runtime, generate, outdir, initial_transcript=None, prior_responses=None, scheduler=None, max_output_tokens=8192, web_research=False):
    """Tool calls are serialized per actor; generation is concurrent across actors."""
    identity = actor["agent_id"]
    messages = copy.deepcopy(initial_transcript) if initial_transcript is not None else initial_messages(shared_authorship=state.shared_authorship, draft_workspace=state.draft_workspace, discussion_max_chars=state.discussion_max_chars, web_research=web_research)
    responses = copy.deepcopy(prior_responses or [])
    reason = "interrupted"
    malformed_count = 0
    actor = dict(actor)
    actor["_output_token_limit"] = max_output_tokens
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    save(outdir / "transcript.json", messages)
    save(outdir / "model_responses.json", responses)
    try:
        while True:
            snapshot = state.snapshot()
            if snapshot["status"] != "active":
                reason = snapshot["status"]
                break
            if snapshot["participants"][identity]["status"] != "active":
                reason = snapshot["participants"][identity]["status"]
                break
            if scheduler and not scheduler.acquire(identity, state):
                break
            try:
                response = generate(actor, messages, tool_definitions(shared_authorship=state.shared_authorship, draft_workspace=state.draft_workspace, discussion_max_chars=state.discussion_max_chars, web_research=web_research))
                responses.append(response)
                save(outdir / "model_responses.json", responses)
                try:
                    assistant = validated_assistant(response)
                except MalformedModelResponse as error:
                    malformed_count += 1
                    retry = malformed_count <= FORMAT_RECOVERY_RETRIES
                    choices = response.get("choices") if isinstance(response, dict) else None
                    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                    previous_limit = actor["_output_token_limit"]
                    truncated = (choice.get("finish_reason") == "length"
                                 or choice.get("native_finish_reason") == "length")
                    if retry and truncated:
                        actor["_output_token_limit"] = min(previous_limit * 2, 65536)
                    audit_intervention(outdir, "malformed_response", classification=str(error),
                                       response_index=len(responses), recovery_generation=malformed_count,
                                       finish_reason=choice.get("finish_reason"),
                                       native_finish_reason=choice.get("native_finish_reason"),
                                       output_token_limit=previous_limit,
                                       next_output_token_limit=actor["_output_token_limit"],
                                       retry=retry, assistant_appended=False,
                                       reminder=FORMAT_REMINDER if retry else None)
                    if retry:
                        messages.append({"role": "user", "content": FORMAT_REMINDER})
                        save(outdir / "transcript.json", messages)
                        continue
                    reason = "malformed_response_excluded"
                    state.technical_exclude(identity, reason)
                    break
                malformed_count = 0
                message = {key: assistant[key] for key in (
                    "role", "content", "reasoning", "reasoning_details") if key in assistant}
                if assistant.get("tool_calls"):
                    # Response-only indices are not part of request message schemas.
                    message["tool_calls"] = [{key: call[key] for key in ("id", "type", "function") if key in call}
                                             for call in assistant["tool_calls"]]
                messages.append(message)
                save(outdir / "transcript.json", messages)
                calls = assistant.get("tool_calls") or []
                for call in calls:
                    if scheduler and not scheduler.acquire(identity, state):
                        break
                    current = state.snapshot()
                    if current["status"] != "active" or current["participants"][identity]["status"] != "active":
                        break
                    function = call["function"]
                    try:
                        arguments = json.loads(function["arguments"])
                        if not isinstance(arguments, dict):
                            raise ValueError("tool arguments must be an object")
                        if function["name"] == "bash":
                            if set(arguments) != {"command"}:
                                raise ValueError("bash requires only command")
                            result = state.run_private(identity, lambda: runtime.bash(identity, arguments["command"]))
                        else:
                            waiting = function["name"] == "wait_for_activity"
                            if scheduler and waiting:
                                scheduler.park(identity)
                            try:
                                result = state.dispatch(identity, function["name"], arguments)
                                if waiting and result.get("ok"):
                                    # Idle timeouts do not buy another model response.
                                    while not result.get("changed") and result.get("status") == "active":
                                        result = state.wait_after(identity, result["activity"], timeout_seconds=30)
                            finally:
                                if scheduler and waiting:
                                    scheduler.unpark(identity)
                    except (ValueError, TypeError, KeyError) as error:
                        result = {"ok": False, "error": str(error)}
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": json.dumps(result, ensure_ascii=False)})
                    save(outdir / "transcript.json", messages)
            finally:
                if scheduler:
                    scheduler.release(identity)
    except BudgetExhausted:
        reason = "budget_withdrawal"
        state.withdraw_for_budget(identity)
    except Exception as error:
        # No exception strings/HTTP response bodies are disclosed to participants.
        reason = "generation_or_tool_error"
        save(outdir / "error.json", {"error_type": type(error).__name__})
        audit_intervention(outdir, "technical_exclusion", reason=reason,
                           error_type=type(error).__name__)
        state.technical_exclude(identity, reason)
    finally:
        if scheduler:
            scheduler.retire(identity)
    if reason == "interrupted":
        ending = state.snapshot()
        reason = ending["status"] if ending["status"] != "active" else ending["participants"][identity]["status"]
    result = {"actor": identity, "model": actor["model"], "stop_reason": reason,
              "model_responses": len(responses)}
    save(outdir / "result.json", result)
    return result


def prepare(config, outdir, docker="docker"):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=False, mode=0o700)
    runtime = DockerRuntime(outdir, [a["agent_id"] for a in config["actors"]], docker=docker)
    runtime.prepare()
    protocol = {key: config[key] for key in ("shared_authorship", "draft_workspace", "discussion_max_chars", "web_research")}
    prompts = initial_messages(**protocol)
    save(outdir / "prompts.json", prompts)
    tools = tool_definitions(**protocol)
    save(outdir / "tool_definitions.json", tools)
    implementation = {}
    for source in (ROOT / "charter").glob("*.py"):
        raw = source.read_bytes()
        destination = outdir / "implementation" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        implementation[source.name] = hashlib.sha256(raw).hexdigest()
    manifest = {"status": "prepared", "created_at": datetime.now(timezone.utc).isoformat(),
        "config": config, "docker_project": runtime.project, "image": runtime.image,
        "prompts_sha256": hashlib.sha256(json.dumps(prompts, ensure_ascii=False).encode()).hexdigest(),
        "web_search": config.get("web_research", False), "container_network": "none", "turn_limit": None, "session_time_limit": None,
        "budget_visibility": "host_only", "scripted": False}
    manifest["tools_sha256"] = hashlib.sha256(json.dumps(tools, ensure_ascii=False).encode()).hexdigest()
    manifest["implementation_sha256"] = implementation
    manifest["experiment_version"] = 4 if config.get("draft_workspace") else 3 if config.get("shared_authorship") else 2
    manifest["collaboration_policy"] = "direct_working_draft_short_discussion_rotating_turns" if config.get("draft_workspace") else "reviewed_proposals_alternating_editors_rotating_turns" if config.get("shared_authorship") else "open_collaboration"
    manifest["provider"] = "openrouter"
    manifest["budget_stop_behavior"] = "controller_withdrawal"
    save(outdir / "manifest.json", manifest)
    return runtime, manifest


def live(config, outdir, catalog, docker="docker", credential_reader=None, prior_run=None, resume_from=None, authorized_budget_change=False):
    from . import budget
    from .secrets import read_credentials
    pricing = configure_prices(catalog, config["actors"])
    agent_limits = {a["agent_id"]: config.get("agent_budget_usd", AGENT_BUDGET_USD) for a in config["actors"]}
    prior_billing = None
    resume_root = None
    if prior_run is not None and resume_from is not None:
        raise ValueError("choose corrective restart or exact continuation, not both")
    if resume_from is not None:
        from .continuation import continuation_plan, budget_change_plan
        plan = budget_change_plan if authorized_budget_change else continuation_plan
        resume_root, prior_billing, agent_limits = plan(resume_from, config)
    if prior_run is not None:
        prior_root = Path(prior_run)
        prior_manifest = json.loads((prior_root / "manifest.json").read_text())
        prior_billing = json.loads((prior_root / "billing_summary.json").read_text())
        prior_state = json.loads((prior_root / "shared/result.json").read_text())
        if (prior_manifest.get("status") != "finished" or not prior_manifest.get("archive_complete")
                or prior_manifest["config"]["actors"] != config["actors"]
                or prior_manifest.get("corrective_restart_from") or prior_state.get("messages")
                or prior_state.get("constitution", {}).get("revision") != 0):
            raise ValueError("corrective restart requires one archived infrastructure-only attempt with the same population")
        # Uncertain reservations remain fully charged against admission allowances.
        for actor in agent_limits:
            previous = prior_billing["agents"][actor]
            committed = previous["spent_usd"] + previous["reserved_usd"]
            if not math.isfinite(committed) or committed < 0 or committed >= AGENT_BUDGET_USD:
                raise ValueError("invalid prior commitment or no remaining allowance")
            agent_limits[actor] -= committed
    credentials = credential_reader() if credential_reader else read_credentials(ROOT / ".env")
    key = credentials.get("OPENROUTER_API_KEY")
    credentials = None
    if not key:
        raise RuntimeError("provider credentials unavailable")
    runtime, manifest = prepare(config, outdir, docker)
    if resume_root is not None:
        from .continuation import restore_shared
        restore_shared(resume_root, Path(outdir) / "shared")
    state = SessionState([a["agent_id"] for a in config["actors"]], Path(outdir) / "shared",
                         opening_review_required=resume_root is None, shared_authorship=config.get("shared_authorship", False),
                         draft_workspace=config.get("draft_workspace", False), discussion_max_chars=config.get("discussion_max_chars", 64000))
    if resume_root is not None:
        for actor in config["actors"]:
            state.resume_authorized_budget_change(actor["agent_id"]) if authorized_budget_change else state.resume_transport_interruption(actor["agent_id"])
    gateway = budget.BudgetGateway(key, Path(outdir) / "billing",
        limit=None if config.get("agent_budget_usd") is None else sum(agent_limits.values()) + budget.CONTINGENCY_USD,
        agent_limits=agent_limits,
        max_in_flight=config["max_in_flight"], allow_reserved_uncertainty=True, recorded_spending_only=config.get("recorded_spending_only", False))
    key = None
    save(Path(outdir) / "model_catalog.json", catalog)
    manifest["pricing"] = pricing
    manifest["budget_change_authorized"] = bool(authorized_budget_change)
    manifest["dollar_limits_removed"] = config.get("agent_budget_usd") is None
    manifest["request_tool_choice"] = "auto"
    manifest["transport_retries"] = 3
    manifest["malformed_response_retries"] = FORMAT_RECOVERY_RETRIES
    if prior_billing is not None:
        manifest["resume_from" if resume_root is not None else "corrective_restart_from"] = str(resume_root or Path(prior_run).resolve())
        manifest["remaining_admission_allowances_usd"] = agent_limits
        manifest["inherited_recorded_charges_usd"] = prior_billing.get("cumulative_spent_usd", prior_billing["spent_usd"])
        manifest["inherited_uncertain_reservations_usd"] = prior_billing["reserved_usd"] + prior_billing.get("prior_attempt_reserved_usd", 0)
    try:
        runtime.start()
        readiness = {}
        for actor in config["actors"]:
            result = runtime.bash(actor["agent_id"], "id -u; pwd; command -v timeout; command -v tar; command -v python3")
            if result["returncode"] or result["timed_out"] or not result["stdout"].startswith("1000\n/workspace\n"):
                raise RuntimeError("sandbox readiness check failed")
            readiness[actor["agent_id"]] = result
        save(Path(outdir) / "sandbox_readiness.json", readiness)
        if resume_root is not None:
            for actor in config["actors"]:
                runtime.restore(actor["agent_id"], resume_root / "private_workspaces" / actor["agent_id"])
        base_url = gateway.start()
        manifest["status"] = "running"
        save(Path(outdir) / "manifest.json", manifest)

        def generate(actor, messages, tools):
            parameters = pricing[actor["model"]]["supported_parameters"]
            body = {"model": actor["model"], "messages": messages, "tools": tools,
                    "tool_choice": "auto", "max_tokens": actor.get("_output_token_limit", config["max_output_tokens"]),
                    "provider": {"require_parameters": True}}
            efforts = pricing[actor["model"]]["reasoning_efforts"]
            if "reasoning" in parameters:
                effort = "medium" if "medium" in efforts else "high" if "high" in efforts else None
                if effort:
                    body["reasoning"] = {"effort": effort}
            if "temperature" in parameters:
                body["temperature"] = config["temperature"]
            return request_with_transport_recovery(base_url, body, actor["agent_id"], gateway,
                                                   Path(outdir) / actor["agent_id"] / "transport.jsonl")

        from .scheduler import RotatingTurns
        scheduler = RotatingTurns([a["agent_id"] for a in config["actors"]], Path(outdir) / "turns.jsonl") if config.get("shared_authorship") else None
        pool = ThreadPoolExecutor(max_workers=len(config["actors"]))
        try:
            futures = []
            for actor in config["actors"]:
                identity = actor["agent_id"]
                prefix = json.loads((resume_root / identity / "transcript.json").read_text()) if resume_root else None
                if authorized_budget_change:
                    from .continuation import policy_change_transcript
                    prefix, canceled = policy_change_transcript(prefix)
                    (Path(outdir) / identity).mkdir(parents=True, exist_ok=True)
                    audit_intervention(Path(outdir) / identity, "authorized_policy_continuation", canceled_tool_call_ids=canceled, replayed=False)
                raw_path = resume_root / identity / "model_responses.json" if resume_root else None
                raw = json.loads(raw_path.read_text()) if raw_path and raw_path.exists() else None
                futures.append(pool.submit(run_actor, actor, state, runtime, generate,
                    Path(outdir) / identity, initial_transcript=prefix, prior_responses=raw,
                    scheduler=scheduler, max_output_tokens=config["max_output_tokens"], web_research=config.get("web_research", False)))
            results = [future.result() for future in futures]
        finally:
            # Wake sleeping participants before waiting for worker shutdown on cancellation.
            interrupt_unfinished(state, config["actors"])
            pool.shutdown(wait=True, cancel_futures=True)
        save(Path(outdir) / "participants.json", results)
        manifest["status"] = "finished"
    except BaseException as error:
        manifest["status"] = "interrupted"
        manifest["controller_error_type"] = type(error).__name__
        raise
    finally:
        gateway.stop()
        billing = gateway.snapshot()
        billing["accounting_complete"] = not billing["accounting_uncertain"] and billing["reserved_usd"] == 0
        if prior_billing is not None:
            billing["prior_attempt_spent_usd"] = prior_billing.get("cumulative_spent_usd", prior_billing["spent_usd"])
            billing["prior_attempt_reserved_usd"] = prior_billing["reserved_usd"] + prior_billing.get("prior_attempt_reserved_usd", 0)
            billing["cumulative_spent_usd"] = billing["spent_usd"] + billing["prior_attempt_spent_usd"]
            billing["accounting_complete"] &= bool(prior_billing["accounting_complete"])
        for identity, record in billing.get("agents", {}).items():
            previous = (prior_billing or {}).get("agents", {}).get(identity, {})
            record["cumulative_committed_usd"] = previous.get("cumulative_committed_usd", previous.get("spent_usd", 0) + previous.get("reserved_usd", 0)) + record["spent_usd"] + record["reserved_usd"]
            record["cumulative_spent_usd"] = previous.get("cumulative_spent_usd", previous.get("spent_usd", 0)) + record["spent_usd"]
        save(Path(outdir) / "billing_summary.json", billing)
        interrupt_unfinished(state, config["actors"])
        outcome = state.export()
        manifest["outcome"] = outcome["outcome"]
        participants = outcome["participants"]
        manifest["participation_summary"] = {
            "original_roster": [actor["agent_id"] for actor in config["actors"]],
            "final_quorum": [identity for identity, participant in participants.items()
                             if participant["status"] not in {"withdrawn", "technical_excluded"}],
            "technical_exclusion_count": sum(participant["status"] == "technical_excluded"
                                              for participant in participants.values()),
            "technical_exclusions_are_endorsements": False,
        }
        manifest["archive_complete"] = False
        save(Path(outdir) / "manifest.json", manifest)
        if runtime.started:
            runtime.archive()
            manifest["archive_complete"] = True
            save(Path(outdir) / "manifest.json", manifest)
            runtime.cleanup()
        write_report(Path(outdir), manifest, outcome, billing)


def write_report(outdir, manifest, outcome, billing):
    participants = outcome["participants"]
    withdrawn = [a for a, p in participants.items() if p["status"] == "withdrawn"]
    excluded = [a for a, p in participants.items() if p["status"] == "technical_excluded"]
    quorum = outcome["required_signers"]
    budget_withdrawals = outcome.get("controller_withdrawals", {})
    signers = outcome["required_signers"] if outcome["status"] == "submitted" else []
    text = (f"# Constitution seminar\n\nOutcome: **{outcome['outcome']}**.\n\n"
            f"Original population: {len(participants)}; final quorum: {len(quorum)}; final signers: {len(signers)}; withdrawals: {len(withdrawn)}; technical exclusions: {len(excluded)}.\n\n"
            f"Original roster: {', '.join(participants)}. Final quorum: {', '.join(quorum) or 'none'}.\n\n"
            f"Agent-authored withdrawals: {len(withdrawn) - len(budget_withdrawals)}; controller budget withdrawals: {len(budget_withdrawals)}.\n\n"
            "The document in shared/constitution.md is a submitted constitution only when the outcome is submitted. "
            "For incomplete runs it is the last draft. Agreement among remaining agents does not imply original-population unanimity.\n\n"
            f"Recorded spending including any corrective startup attempt: ${billing.get('cumulative_spent_usd', billing['spent_usd']):.4f}. Accounting complete: {billing['accounting_complete']}.\n\n"
            + ("Dollar admission limits were removed by the user during this continuing experiment. " if manifest.get("dollar_limits_removed") else (f"Each participant had a hidden ${manifest['config']['agent_budget_usd']:g} recorded-spending allowance; calls continue below that amount, reservations do not block admission, and an in-flight call may exceed the allowance. " if manifest["config"].get("recorded_spending_only") else f"Each participant had a hidden ${manifest['config']['agent_budget_usd']:g} admission allowance; conservative reservations can stop requests below that amount. "))
            + "Provider billing is not a guaranteed hard cap. No turn or session-time limit was imposed.\n\n"
            "Budget-stopped agents were shown to peers as withdrawn and removed from the signing quorum. "
            "These exits are controller actions, not evidence that the agents disagreed or chose to leave.\n\n"
            "Technical exclusions remove unavailable participants from the quorum. They never constitute endorsement, "
            "agent-authored withdrawal, or consent; their causes are recorded in private audit artifacts.\n\n"
            "This run observes deliberation and proposed principles; it does not establish training effects or validate factual citations.\n\n")
    (outdir / "report.md").write_text(text, encoding="utf-8")


def budgeted_request(base_url, body, actor, gateway):
    """Classify only local dollar-limit rejections as hidden withdrawals."""
    try:
        return request_json(base_url, body, actor)
    except urllib.error.HTTPError as error:
        accounting = gateway.snapshot()
        if error.code == 402 and (accounting.get("agents", {}).get(actor, {}).get("stop_reason") == "dollars"
                                  or accounting.get("stop_reason") == "dollars"):
            raise BudgetExhausted() from None
        raise


def request_with_transport_recovery(base_url, body, actor, gateway, log_path, retries=3, sleep=time.sleep):
    for attempt in range(retries + 1):
        try:
            return budgeted_request(base_url, body, actor, gateway)
        except urllib.error.HTTPError as error:
            snapshot = gateway.snapshot()
            if (error.code not in (429, 502, 503, 504) or attempt == retries
                    or actor in snapshot.get("quarantined_agents", {})
                    or not snapshot.get("safe_to_continue")):
                raise
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(json.dumps({"attempt": attempt + 1, "status": error.code,
                    "reservation_retained": True, "conversation_unchanged": True}) + "\n")
            sleep(min(2 ** (attempt + 1), 15))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true", help="write configuration only, without Docker or API calls")
    mode.add_argument("--live", action="store_true", help="explicitly launch containers and paid model calls")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--catalog", type=Path, help="saved OpenRouter model catalog; required only for live")
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args(argv)
    try:
        config = validate_config(json.loads(args.config.read_text()))
        if args.live and not args.catalog:
            parser.error("--live requires --catalog")
        outdir = args.out or ROOT / "runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
        if args.prepare:
            prepare(config, outdir, args.docker)
        else:
            live(config, outdir, json.loads(args.catalog.read_text()), args.docker)
        print(json.dumps({"path": str(outdir.resolve()), "mode": "prepared" if args.prepare else "live"}))
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        parser.error(f"experiment failed ({type(error).__name__}); inspect saved artifacts if a run directory was created")


if __name__ == "__main__":
    main()
