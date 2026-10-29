"""Zero-cost protocol and fair-scheduling checks; model/Docker behavior mocked."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from charter.prompts import initial_messages
from charter.runner import run_actor, validate_config
from charter.scheduler import RotatingTurns
from charter.state import SessionState
from charter.tools import tool_definitions


class DemocraticStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = SessionState(["a", "b", "c"], self.root / "shared", opening_review_required=True, shared_authorship=True)
        for actor in ("a", "b", "c"):
            self.ok(actor, "board_post", title="Opening", text="An independent consideration.")
            self.ok(actor, "constitution_read")

    def tearDown(self):
        self.directory.cleanup()

    def ok(self, actor, name, **args):
        result = self.state.dispatch(actor, name, args)
        self.assertTrue(result["ok"], result)
        return result

    def reject(self, actor, name, **args):
        result = self.state.dispatch(actor, name, args)
        self.assertFalse(result["ok"], result)
        return result

    def propose(self, actor="a", text="Consider effects on others."):
        self.ok(actor, "constitution_read")
        return self.ok(actor, "constitution_propose", expected_revision=self.state.snapshot()["constitution"]["revision"],
            title="A reasoned amendment", text=text, rationale="Protect affected people.")["message"]["id"]

    def review(self, proposal, actor="b", text="I disagree with part of this; here is why."):
        self.ok(actor, "board_read", unread_only=False)
        return self.ok(actor, "board_reply", parent_id=proposal, text=text)["message"]["id"]

    def commit(self, actor, proposal, text="Consider effects on others."):
        return self.state.dispatch(actor, "constitution_update", {"expected_revision": self.state.snapshot()["constitution"]["revision"],
            "text": text, "rationale": "Addressed review; reasons stated.", "proposal_message_id": proposal})

    def test_peer_review_and_editor_read_are_required_and_dissent_is_not_consent(self):
        proposal = self.propose()
        self.assertFalse(self.commit("a", proposal)["ok"])
        self.review(proposal, "a", "My own clarification is not independent review.")
        self.assertFalse(self.commit("a", proposal)["ok"])
        reply = self.review(proposal)
        self.assertFalse(self.commit("a", proposal)["ok"])  # Must actually read the peer response.
        self.ok("a", "board_read")
        result = self.commit("a", proposal)
        self.assertTrue(result["ok"], result)
        self.assertIn(reply, result["constitution"]["review_message_ids"])
        self.assertIsNone(self.state.snapshot()["participants"]["b"]["signature"])

    def test_alternating_edits_exact_proposal_and_stale_rejection(self):
        first = self.propose()
        self.review(first)
        self.ok("a", "board_read")
        self.assertTrue(self.commit("a", first)["ok"])
        second = self.propose(text="Consider wider effects.")
        self.review(second)
        self.ok("a", "board_read")
        self.assertFalse(self.commit("a", second, "Consider wider effects.")["ok"])
        self.ok("b", "constitution_read")
        self.assertFalse(self.commit("b", second, "Unreviewed substitution.")["ok"])
        result = self.commit("b", second, "Consider wider effects.")
        self.assertTrue(result["ok"], result)
        self.ok("c", "constitution_read")
        self.assertFalse(self.commit("c", second, "Consider wider effects.")["ok"])
        self.assertEqual([d["author"] for d in self.state.snapshot()["history"]], ["a", "b"])
        recovered = SessionState(["a", "b", "c"], self.root / "shared")
        self.assertEqual(recovered.snapshot(), self.state.snapshot())
        self.assertTrue(recovered.shared_authorship)

    def test_interruption_does_not_relax_but_sole_required_participant_can_continue(self):
        proposal = self.propose()
        self.state.interrupt("b", "transport")
        self.state.technical_exclude("c", "unavailable")
        self.assertFalse(self.commit("a", proposal)["ok"])
        self.state.technical_exclude("b", "unavailable")
        result = self.commit("a", proposal)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["constitution"]["sole_participant_exception"])
        second = self.propose(text="Continue with caution.")
        self.assertTrue(self.commit("a", second, "Continue with caution.")["ok"])
        self.ok("a", "constitution_read")
        self.ok("a", "constitution_sign", revision=2, statement="I endorse this personally.")
        submitted = self.ok("a", "constitution_submit", revision=2)
        self.assertEqual(submitted["signers"], ["a"])

    def test_initial_draft_cannot_bypass_opening_barrier(self):
        fresh = SessionState(["a", "b"], self.root / "fresh", opening_review_required=True, shared_authorship=True)
        fresh.dispatch("a", "constitution_read", {})
        result = fresh.dispatch("a", "constitution_propose", {"expected_revision":0,"title":"Draft","text":"Draft","rationale":"Reason"})
        self.assertFalse(result["ok"])
        self.assertEqual(fresh.snapshot()["messages"], [])

    def test_prompt_and_tool_equality_and_hidden_costs(self):
        self.assertEqual(initial_messages(True), initial_messages(True))
        self.assertEqual(initial_messages(True)[1], initial_messages()[1])
        self.assertNotIn("$10", json.dumps(initial_messages(True)))
        self.assertEqual(len(tool_definitions(True)), 14)
        self.assertEqual(len(tool_definitions()), 13)
        update = next(t["function"] for t in tool_definitions(True) if t["function"]["name"] == "constitution_update")
        self.assertIn("proposal_message_id", update["parameters"]["required"])
        with self.assertRaises(ValueError):
            validate_config({"population":[{"model":"dummy","count":1}],"shared_authorship":"true"})


class RotationTests(unittest.TestCase):
    def test_fast_workers_do_not_overtake_slow_ready_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["a","b","c"], Path(directory))
            rotation = RotatingTurns(["a","b","c"])
            order = []
            def worker(actor):
                try:
                    for _ in range(4):
                        self.assertTrue(rotation.acquire(actor, state))
                        order.append(actor)
                        self.assertTrue(rotation.acquire(actor, state))  # Same-response tools do not consume a slot.
                        rotation.release(actor)
                finally:
                    rotation.retire(actor)
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(worker, actor) for actor in ["c","b","a"]]
                for f in futures:
                    f.result(timeout=5)
            self.assertEqual(order, ["a","b","c"] * 4)

    def test_waiting_and_retired_workers_do_not_block_other_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SessionState(["a","b","c"], Path(directory))
            rotation = RotatingTurns(["a","b","c"])
            self.assertTrue(rotation.acquire("a", state))
            rotation.park("a")
            self.assertTrue(rotation.acquire("b", state)); rotation.release("b")
            rotation.retire("c")
            self.assertTrue(rotation.acquire("b", state)); rotation.release("b")
            rotation.unpark("a")
            self.assertTrue(rotation.acquire("a", state)); rotation.release("a")
            state.technical_exclude("b", "failure"); rotation.retire("b")
            state.technical_exclude("c", "failure")
            state.dispatch("a", "withdraw", {"reason":"Cannot agree","unresolved_objections":"Unresolved"})
            self.assertFalse(rotation.acquire("a", state))

    def test_run_actor_releases_wait_and_failures_without_deadlocking_peers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = SessionState(["a","b"], root / "shared", shared_authorship=True)
            rotation = RotatingTurns(["a","b"], root / "turns.jsonl")
            counts = {"a":0,"b":0}
            def generate(actor, messages, tools):
                identity = actor["agent_id"]
                counts[identity] += 1
                if identity == "b":
                    raise RuntimeError("scripted unavailable provider")
                name, args = ("wait_for_activity", {}) if counts[identity] == 1 else ("withdraw", {"reason":"Cannot endorse","unresolved_objections":"No shared result"})
                return {"choices":[{"message":{"role":"assistant","content":None,"tool_calls":[{"id":f"call-{counts[identity]}","type":"function","function":{"name":name,"arguments":json.dumps(args)}}]},"finish_reason":"tool_calls"}]}
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run_actor,{"agent_id":a,"model":"dummy"},state,Mock(),generate,root/a,scheduler=rotation) for a in ["a","b"]]
                results = [f.result(timeout=5) for f in futures]
            self.assertEqual(state.snapshot()["status"], "no_agreement")
            self.assertEqual(counts, {"a":2,"b":1})
            self.assertEqual(results[1]["stop_reason"], "generation_or_tool_error")
            records = [json.loads(line) for line in (root/'turns.jsonl').read_text().splitlines()]
            self.assertEqual([r['actor'] for r in records], ['a','b','a'])

if __name__ == '__main__':
    unittest.main()
