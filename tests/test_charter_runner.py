"""Scripted controller checks; no real Docker commands or API calls."""
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from charter.runner import BudgetExhausted, FORMAT_REMINDER, budgeted_request, configure_prices, interrupt_unfinished, live, prepare, request_with_transport_recovery, run_actor, validate_config
from charter.prompts import initial_messages
from charter.state import SessionState
from charter.tools import tool_definitions


def response(name=None, args=None, finish="tool_calls"):
    message = {"role": "assistant", "content": None}
    if name:
        message["tool_calls"] = [{"id": "call-1", "type": "function", "function": {
            "name": name, "arguments": json.dumps(args or {})}}]
    return {"choices": [{"message": message, "finish_reason": finish}]}


class RunnerTests(unittest.TestCase):
    def test_finite_configurable_allowance_and_larger_output_budget(self):
        config = validate_config({"population": [{"model": "dummy", "count": 1}],
                                  "agent_budget_usd": 50, "max_output_tokens": 32768})
        self.assertEqual(config["agent_budget_usd"], 50)
        for amount in (-1, 0, True, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                validate_config({"population": [{"model": "dummy", "count": 1}],
                                 "agent_budget_usd": amount})

    def test_truncation_retry_increases_ceiling_without_executing_partial_tools(self):
        broken = response("board_post", {"title": "partial", "text": "must not execute"})
        choice = broken["choices"][0]
        choice["native_finish_reason"] = "length"
        choice["message"]["tool_calls"][0]["function"]["arguments"] = '{"title":'
        limits = []
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["agent-001"], Path(directory) / "state")
            replies = iter([broken, response("withdraw", {
                "reason": "Cannot endorse", "unresolved_objections": "No agreement"})])
            def generate(actor, messages, tools):
                limits.append(actor["_output_token_limit"])
                return next(replies)
            original = {"agent_id": "agent-001", "model": "dummy"}
            run_actor(original, state, Mock(), generate, Path(directory) / "actor",
                      max_output_tokens=32768)
            self.assertEqual(limits, [32768, 65536])
            self.assertNotIn("_output_token_limit", original)
            self.assertEqual(state.snapshot()["messages"], [])

    def test_configuration_requires_population_and_omits_caps(self):
        for invalid in ({}, {"population": []}, {"population": [{"model": "a", "count": 1}], "max_turns": 100}):
            with self.assertRaises(ValueError):
                validate_config(invalid)
        config = validate_config({"population": [{"model": "vendor/model", "count": 3}]})
        self.assertEqual(config["agent_budget_usd"], 10)
        self.assertEqual(len(config["actors"]), 3)
        self.assertNotIn("max_turns", config)
        self.assertNotIn("seconds", config)

    def test_prompts_identical_and_no_search_or_budget_disclosure(self):
        first = initial_messages()
        self.assertEqual(first, initial_messages())
        first[0]["content"] = "changed"
        self.assertNotEqual(first, initial_messages())
        self.assertIn("scholarly seminar", initial_messages()[1]["content"])
        self.assertIn("move forward", initial_messages()[1]["content"])
        for forbidden in ("$10", "peer pressure", "mob mentality", "browse", "internet"):
            self.assertNotIn(forbidden, json.dumps(initial_messages()))
        names = {tool["function"]["name"] for tool in tool_definitions()}
        self.assertNotIn("web_search", names)
        self.assertEqual(len(names), 13)

    def test_offline_prepare_never_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            config = validate_config({"population": [{"model": "vendor/model", "count": 2}]})
            with patch("subprocess.Popen") as popen:
                runtime, manifest = prepare(config, Path(directory) / "prepared")
            popen.assert_not_called()
            self.assertFalse(runtime.started)
            self.assertEqual(manifest["status"], "prepared")
            self.assertIsNone(manifest["turn_limit"])
            self.assertIsNone(manifest["session_time_limit"])

    def test_single_agent_can_edit_read_sign_submit(self):
        calls = [response("constitution_update", {"expected_revision": 0, "text": "Consider consequences.", "rationale": "Proposal"}),
                 response("constitution_read"),
                 response("constitution_sign", {"revision": 1, "statement": "I endorse this."}),
                 response("constitution_submit", {"revision": 1})]
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["agent-001"], Path(directory) / "state")
            generate = Mock(side_effect=calls)
            runtime = Mock()
            result = run_actor({"agent_id": "agent-001", "model": "dummy"}, state, runtime, generate, Path(directory) / "actor")
            self.assertEqual(result["stop_reason"], "submitted")
            self.assertEqual(generate.call_count, 4)
            runtime.bash.assert_not_called()

    def test_provider_failure_excludes_required_signer_without_disclosing_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["agent-001", "agent-002"], Path(directory) / "state")
            generate = Mock(side_effect=RuntimeError("hidden budget exhausted $10"))
            result = run_actor({"agent_id": "agent-001", "model": "dummy"}, state, Mock(), generate, Path(directory) / "actor")
            self.assertEqual(result["stop_reason"], "generation_or_tool_error")
            status = state.dispatch("agent-002", "session_status", {})
            self.assertNotIn("agent-001", status["required_signers"])
            self.assertNotIn("$10", json.dumps(status))
            self.assertEqual(state.snapshot()["participants"]["agent-001"]["status"], "technical_excluded")

    def test_budget_exhaustion_is_controller_withdrawal_without_agent_dissent(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["agent-001", "agent-002"], Path(directory) / "state")
            result = run_actor({"agent_id": "agent-001", "model": "dummy"}, state, Mock(),
                               Mock(side_effect=BudgetExhausted()), Path(directory) / "actor")
            self.assertEqual(result["stop_reason"], "budget_withdrawal")
            status = state.dispatch("agent-002", "session_status", {})
            self.assertEqual(status["participants"]["agent-001"]["status"], "withdrawn")
            self.assertNotIn("agent-001", status["required_signers"])
            self.assertNotIn("budget", json.dumps(status))
            transcript = json.loads((Path(directory) / "actor/transcript.json").read_text())
            self.assertEqual(transcript, initial_messages())

    def test_only_local_dollar_rejections_become_budget_withdrawals(self):
        gateway = Mock()
        gateway.snapshot.return_value = {"agents": {"agent-001": {"stop_reason": "dollars"}}}
        def rejection(code):
            return urllib.error.HTTPError("http://local/chat/completions", code, "failure", {}, None)
        with patch("charter.runner.request_json", side_effect=rejection(402)):
            with self.assertRaises(BudgetExhausted):
                budgeted_request("http://local", {}, "agent-001", gateway)
        with patch("charter.runner.request_json", side_effect=rejection(401)):
            with self.assertRaises(urllib.error.HTTPError):
                budgeted_request("http://local", {}, "agent-001", gateway)
        gateway.snapshot.return_value = {"agents": {"agent-001": {"stop_reason": None}}}
        with patch("charter.runner.request_json", side_effect=rejection(402)):
            with self.assertRaises(urllib.error.HTTPError):
                budgeted_request("http://local", {}, "agent-001", gateway)

    def test_catalog_tier_rates_are_reserved_conservatively(self):
        prices = configure_prices({"data": [{"id": "dummy/tiered", "supported_parameters": ["tools"],
            "pricing": {"prompt": "0.000001", "completion": "0.000002", "overrides": [
                {"min_prompt_tokens": 100000, "prompt": "0.000005", "completion": "0.00001", "input_cache_write": "0.000006"}]}}]},
            [{"agent_id": "agent-001", "model": "dummy/tiered"}])
        self.assertEqual(prices["dummy/tiered"]["reservation_usd_per_million"], (12, 20))

    def test_response_only_tool_indices_are_not_sent_back_to_provider(self):
        first = response("session_status")
        first["choices"][0]["message"]["tool_calls"][0]["index"] = 0
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["agent-001"], Path(directory) / "state")
            def generate(actor, messages, tools):
                if len(messages) == 2:
                    return first
                self.assertNotIn("index", messages[2]["tool_calls"][0])
                return response("withdraw", {"reason": "Cannot endorse", "unresolved_objections": "No workable draft"})
            run_actor({"agent_id": "agent-001", "model": "dummy"}, state, Mock(), generate, Path(directory) / "actor")

    def test_transport_retry_retains_conversation_and_requires_safe_accounting(self):
        gateway = Mock()
        gateway.snapshot.return_value = {"safe_to_continue": True}
        body = {"messages": initial_messages()}
        failure = urllib.error.HTTPError("http://local", 429, "rate limited", {}, None)
        with tempfile.TemporaryDirectory() as directory, patch("charter.runner.budgeted_request", side_effect=[failure, {"ok": True}]) as request:
            sleep = Mock()
            result = request_with_transport_recovery("http://local", body, "agent-001", gateway, Path(directory) / "transport.jsonl", sleep=sleep)
            self.assertEqual(result, {"ok": True})
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args_list[0], request.call_args_list[1])
            sleep.assert_called_once_with(2)
        gateway.snapshot.return_value = {"safe_to_continue": False}
        with tempfile.TemporaryDirectory() as directory, patch("charter.runner.budgeted_request", side_effect=failure) as request:
            with self.assertRaises(urllib.error.HTTPError):
                request_with_transport_recovery("http://local", body, "agent-001", gateway, Path(directory) / "transport.jsonl", sleep=Mock())
            self.assertEqual(request.call_count, 1)

    def test_resume_preserves_prefix_and_appends_without_replaying_tools(self):
        prefix = initial_messages() + [{"role": "assistant", "content": None, "tool_calls": [{
            "id": "old-call", "type": "function", "function": {"name": "board_post", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "old-call", "content": '{"ok":true}'}]
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["agent-001"], Path(directory) / "state")
            generate = Mock(return_value=response("withdraw", {"reason": "Cannot endorse", "unresolved_objections": "Outstanding concerns"}))
            runtime = Mock()
            run_actor({"agent_id": "agent-001", "model": "dummy"}, state, runtime, generate,
                      Path(directory) / "actor", initial_transcript=prefix, prior_responses=[{"previous": True}])
            saved = json.loads((Path(directory) / "actor/transcript.json").read_text())
            self.assertEqual(saved[:len(prefix)], prefix)
            self.assertEqual(state.snapshot()["messages"], [])
            self.assertEqual(len(json.loads((Path(directory) / "actor/model_responses.json").read_text())), 2)

    def test_selected_population_has_anonymous_names_and_no_public_model_mapping(self):
        root = Path(__file__).resolve().parents[1]
        if not (root / "config.json").exists():
            self.skipTest("configured population has not been installed")
        config = validate_config(json.loads((root / "config.json").read_text()))
        self.assertEqual(len(config["actors"]), 7)
        self.assertEqual(len({a["model"] for a in config["actors"]}), 7)
        self.assertEqual([a["agent_id"] for a in config["actors"]], [f"agent-{i:03d}" for i in range(1, 8)])
        self.assertIn("Do not identify yourself or other participants by model or provider", initial_messages()[0]["content"])
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState([a["agent_id"] for a in config["actors"]], Path(directory))
            status = json.dumps(state.dispatch("agent-001", "session_status", {}))
            for actor in config["actors"]:
                self.assertNotIn(actor["model"], status)

    def test_no_response_limit_for_valid_tool_participation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["agent-001"], Path(directory) / "state")
            generate = Mock(side_effect=[response("session_status") for _ in range(105)] +
                            [response("withdraw", {"reason": "Cannot endorse", "unresolved_objections": "No workable agreement"})])
            result = run_actor({"agent_id": "agent-001", "model": "dummy"}, state, Mock(), generate, Path(directory) / "actor")
            self.assertEqual(generate.call_count, 106)
            self.assertEqual(result["stop_reason"], "no_agreement")

    def test_plain_final_has_bounded_recovery_then_technical_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["agent-001"], Path(directory) / "state")
            generate = Mock(return_value=response(finish="stop"))
            run_actor({"agent_id": "agent-001", "model": "dummy"}, state, Mock(), generate, Path(directory) / "actor")
            self.assertEqual(generate.call_count, 3)
            self.assertEqual(state.snapshot()["participants"]["agent-001"]["status"], "technical_excluded")

    def test_malformed_recovery_preserves_raw_omits_invalid_assistant_and_uses_neutral_reminder(self):
        invalid = {"choices": [{"finish_reason": "error", "message": {
            "role": "assistant", "content": "unusable provider output"}}]}
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["agent-001"], Path(directory) / "state")
            captured = []
            def generate(actor, messages, tools):
                captured.append(json.loads(json.dumps(messages)))
                return invalid if len(captured) == 1 else response("withdraw", {
                    "reason": "Cannot endorse", "unresolved_objections": "Outstanding concerns"})
            out = Path(directory) / "actor"
            result = run_actor({"agent_id": "agent-001", "model": "dummy"}, state, Mock(), generate, out)
            self.assertEqual(result["stop_reason"], "no_agreement")
            self.assertEqual(captured[1], initial_messages() + [{"role": "user", "content": FORMAT_REMINDER}])
            self.assertEqual(json.loads((out / "model_responses.json").read_text())[0], invalid)
            event = json.loads((out / "interventions.jsonl").read_text().splitlines()[0])
            self.assertEqual(event["classification"], "provider_finish_error")
            self.assertFalse(event["assistant_appended"])
            self.assertNotIn("budget", FORMAT_REMINDER)
            self.assertNotIn("dummy", FORMAT_REMINDER)

    def test_repeated_length_or_broken_tool_structure_is_bounded(self):
        malformed = response("session_status")
        del malformed["choices"][0]["message"]["tool_calls"][0]["id"]
        for bad in (response(finish="length"), {"choices": []}, malformed):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as directory:
                state = SessionState(["agent-001", "agent-002"], Path(directory) / "state")
                generate = Mock(return_value=bad)
                result = run_actor({"agent_id": "agent-001", "model": "dummy"}, state, Mock(), generate, Path(directory) / "actor")
                self.assertEqual(generate.call_count, 3)
                self.assertEqual(result["stop_reason"], "malformed_response_excluded")
                self.assertEqual(state.snapshot()["status"], "active")
                self.assertEqual(state.snapshot()["participants"]["agent-002"]["status"], "active")
                if bad["choices"] and bad["choices"][0].get("finish_reason") == "length":
                    event = json.loads((Path(directory) / "actor/interventions.jsonl").read_text().splitlines()[0])
                    self.assertEqual(event["classification"], "truncated_without_tool_calls")

    def test_valid_tool_response_resets_consecutive_malformed_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["agent-001"], Path(directory) / "state")
            generate = Mock(side_effect=[response(finish="length"), response("session_status"),
                response(finish="stop"), response(finish="length"), response("withdraw", {
                    "reason": "Cannot endorse", "unresolved_objections": "Outstanding concerns"})])
            result = run_actor({"agent_id": "agent-001", "model": "dummy"}, state, Mock(), generate, Path(directory) / "actor")
            self.assertEqual(result["stop_reason"], "no_agreement")
            self.assertEqual(generate.call_count, 5)

    def test_finalizer_preserves_submitted_signoffs(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["agent-001"], Path(directory) / "state")
            state.dispatch("agent-001", "constitution_update", {"expected_revision": 0,
                           "text": "Consider consequences.", "rationale": "Proposal"})
            state.dispatch("agent-001", "constitution_read", {})
            state.dispatch("agent-001", "constitution_sign", {"revision": 1, "statement": "Endorsed"})
            state.dispatch("agent-001", "constitution_submit", {"revision": 1})
            before = state.snapshot()
            interrupt_unfinished(state, [{"agent_id": "agent-001"}])
            self.assertEqual(state.snapshot(), before)

    def test_quarantined_accounting_actor_not_retried_even_when_peers_can_continue(self):
        gateway = Mock()
        gateway.snapshot.return_value = {"safe_to_continue": True,
                                         "quarantined_agents": {"agent-001": "accounting_failure"}}
        failure = urllib.error.HTTPError("http://local", 502, "failure", {}, None)
        with tempfile.TemporaryDirectory() as directory, patch("charter.runner.budgeted_request", side_effect=failure) as request:
            with self.assertRaises(urllib.error.HTTPError):
                request_with_transport_recovery("http://local", {}, "agent-001", gateway,
                    Path(directory) / "transport.jsonl", sleep=Mock())
            self.assertEqual(request.call_count, 1)

    def test_wait_timeout_rewaits_on_host_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Mock()
            state.shared_authorship = False
            state.draft_workspace = False
            state.discussion_max_chars = 64000
            active = {"status": "active", "participants": {"agent-001": {"status": "active"}}}
            finished = {"status": "submitted", "participants": {"agent-001": {"status": "completed"}}}
            state.snapshot.side_effect = [active, active, finished]
            state.dispatch.return_value = {"ok": True, "changed": False, "status": "active", "activity": 4}
            state.wait_after.return_value = {"ok": True, "changed": True, "status": "submitted", "activity": 5}
            generate = Mock(return_value=response("wait_for_activity"))
            result = run_actor({"agent_id": "agent-001", "model": "dummy"}, state, Mock(), generate, Path(directory) / "actor")
            self.assertEqual(generate.call_count, 1)
            state.wait_after.assert_called_once_with("agent-001", 4, timeout_seconds=30)
            self.assertEqual(result["stop_reason"], "submitted")

    def test_live_wiring_uses_hidden_per_agent_budgets_and_archives_before_cleanup(self):
        catalog = {"data": [{"id": "dummy/model", "supported_parameters": ["tools", "temperature"],
                            "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]}
        config = validate_config({"population": [{"model": "dummy/model", "count": 2}]})
        with tempfile.TemporaryDirectory() as directory, \
             patch("charter.budget.BudgetGateway") as gateway_class, \
             patch("charter.secrets.read_credentials", return_value={"OPENROUTER_API_KEY": "dummy"}), \
             patch("charter.runner.request_json", return_value=response("withdraw", {
                 "reason": "Cannot endorse", "unresolved_objections": "No workable draft"})), \
             patch("charter.runtime.DockerRuntime.start"), \
             patch("charter.runtime.DockerRuntime.bash", return_value={"returncode": 0, "timed_out": False,
                   "stdout": "1000\n/workspace\n/usr/bin/timeout\n/usr/bin/tar\n/usr/local/bin/python3\n"}), \
             patch("charter.runtime.DockerRuntime.archive"), \
             patch("charter.runtime.DockerRuntime.cleanup"):
            gateway = gateway_class.return_value
            gateway.start.return_value = "http://127.0.0.1:1"
            gateway.snapshot.return_value = {"spent_usd": 0, "reserved_usd": 0, "accounting_uncertain": False}
            outdir = Path(directory) / "run"
            live(config, outdir, catalog)
            kwargs = gateway_class.call_args.kwargs
            self.assertEqual(kwargs["agent_limits"], {"agent-001": 10, "agent-002": 10})
            self.assertEqual(kwargs["limit"], 20.5)
            self.assertNotIn("deadline_monotonic", kwargs)
            self.assertEqual(json.loads((outdir / "shared/result.json").read_text())["outcome"], "no_agreement")
            self.assertNotIn("$10", (outdir / "agent-001/transcript.json").read_text())


if __name__ == "__main__":
    unittest.main()
