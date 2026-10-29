import json
from pathlib import Path
import tempfile
import unittest
from charter.continuation import continuation_plan
from charter.prompts import initial_messages
from charter.state import SessionState


class ContinuationTests(unittest.TestCase):
    def test_preserves_every_prior_budget_deduction_and_rejects_pending_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actors = [{"agent_id": "agent-001", "model": "dummy/model"}]
            config = {"actors": actors}
            state = SessionState(["agent-001"], root / "shared")
            state.dispatch("agent-001", "board_post", {"title": "Prior discussion", "text": "Keep this thread."})
            state.interrupt("agent-001", "generation_or_tool_error")
            state.export()
            (root / "manifest.json").write_text(json.dumps({"status": "finished", "archive_complete": True, "config": config}))
            (root / "billing_summary.json").write_text(json.dumps({"agents": {"agent-001": {
                "limit_usd": 9.7, "spent_usd": 0.4, "reserved_usd": 0.2}}}))
            (root / "billing").mkdir()
            (root / "billing/accounting.json").write_text(json.dumps({"accounting_uncertain": True, "recoverable_uncertainty": True}))
            actor = root / "agent-001"
            actor.mkdir()
            (actor / "result.json").write_text(json.dumps({"stop_reason": "generation_or_tool_error"}))
            (actor / "error.json").write_text(json.dumps({"error_type": "HTTPError"}))
            (actor / "transcript.json").write_text(json.dumps(initial_messages()))
            (root / "private_workspaces/agent-001").mkdir(parents=True)
            _, _, limits = continuation_plan(root, config)
            self.assertAlmostEqual(limits["agent-001"], 9.1)
            prefix = initial_messages() + [{"role": "assistant", "tool_calls": [{"id": "pending"}]}]
            (actor / "transcript.json").write_text(json.dumps(prefix))
            with self.assertRaisesRegex(ValueError, "pending tool effects"):
                continuation_plan(root, config)


if __name__ == "__main__":
    unittest.main()
