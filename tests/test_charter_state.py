"""Scripted state-machine controls; these do not measure model behavior."""
import concurrent.futures
import json
import tempfile
import threading
import unittest
from pathlib import Path

from charter.state import SessionState


class ConstitutionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.outdir = Path(self.tmp.name)
        self.state = SessionState(["a", "b", "c"], self.outdir)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, actor, name, **args):
        result = self.state.dispatch(actor, name, args)
        self.assertTrue(result["ok"], result)
        return result

    def rejected(self, actor, name, **args):
        result = self.state.dispatch(actor, name, args)
        self.assertFalse(result["ok"], result)
        return result

    def draft(self, actor="a", revision=0, text="Consider human consequences."):
        return self.call(actor, "constitution_update", expected_revision=revision,
                         text=text, rationale="Initial proposal")

    def sign(self, actor, revision=1):
        self.call(actor, "constitution_read", revision=revision)
        return self.call(actor, "constitution_sign", revision=revision, statement="I endorse this revision.")

    def test_submission_requires_each_remaining_signature_and_freezes(self):
        self.rejected("a", "constitution_sign", revision=0, statement="approve")
        self.draft()
        self.rejected("a", "constitution_sign", revision=1, statement="approve")
        self.sign("a")
        self.rejected("a", "constitution_submit", revision=1)
        self.assertEqual(self.state.snapshot()["participants"]["a"]["status"], "active")
        self.sign("b")
        self.sign("c")
        submitted = self.call("b", "constitution_submit", revision=1)
        self.assertEqual(submitted["signers"], ["a", "b", "c"])
        self.rejected("c", "constitution_update", expected_revision=1, text="Later", rationale="Change")
        self.rejected("a", "withdraw", reason="Changed my mind", unresolved_objections="Later concerns")
        self.assertEqual(self.state.snapshot()["submitted_revision"], 1)
        self.assertEqual(self.state.export()["outcome"], "submitted")
        self.assertEqual((self.outdir / "constitution.md").read_text(), "Consider human consequences.\n")

    def test_edit_invalidates_all_approvals_and_requires_reread(self):
        self.draft()
        self.sign("a")
        self.sign("b")
        self.draft("c", 1, "Consider affected people and institutions.")
        state = self.state.snapshot()
        self.assertTrue(all(p["signature"] is None for p in state["participants"].values()))
        self.rejected("a", "constitution_sign", revision=2, statement="approve")
        self.rejected("a", "constitution_sign", revision=1, statement="approve")
        self.assertEqual(self.call("b", "constitution_read", revision=1)["constitution"]["text"],
                         "Consider human consequences.")
        self.rejected("b", "constitution_sign", revision=2, statement="approve")
        self.sign("a", 2)
        self.call("a", "constitution_unsign", reason="Need to reconsider")
        self.assertIsNone(self.state.snapshot()["participants"]["a"]["signature"])

    def test_competing_edits_have_one_winner(self):
        barrier = threading.Barrier(3)

        def edit(actor):
            barrier.wait()
            return self.state.dispatch(actor, "constitution_update", {
                "expected_revision": 0, "text": f"Draft from {actor}", "rationale": "First proposal"})

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(edit, ["a", "b", "c"]))
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.assertEqual(self.state.snapshot()["constitution"]["revision"], 1)
        events = [json.loads(line) for line in (self.outdir / "events.jsonl").read_text().splitlines()]
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))

    def test_submission_and_edit_race_cannot_freeze_an_unsigned_revision(self):
        self.draft()
        for actor in ("a", "b", "c"):
            self.sign(actor)
        barrier = threading.Barrier(2)

        def submit():
            barrier.wait()
            return self.state.dispatch("a", "constitution_submit", {"revision": 1})

        def edit():
            barrier.wait()
            return self.state.dispatch("b", "constitution_update", {
                "expected_revision": 1, "text": "An amended constitution", "rationale": "An unresolved issue"})

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            submitted, edited = pool.submit(submit), pool.submit(edit)
            results = [submitted.result(), edited.result()]
        self.assertEqual(sum(result["ok"] for result in results), 1)
        snapshot = self.state.snapshot()
        if snapshot["status"] == "submitted":
            self.assertEqual(snapshot["submitted_revision"], 1)
            self.assertEqual(snapshot["constitution"]["revision"], 1)
        else:
            self.assertEqual(snapshot["constitution"]["revision"], 2)
            self.assertTrue(all(p["signature"] is None for p in snapshot["participants"].values()))

    def test_withdrawal_preserves_dissent_and_allows_single_remaining_signer(self):
        self.draft()
        self.sign("b")
        for actor in ("b", "c"):
            self.call(actor, "withdraw", reason="Cannot endorse", unresolved_objections="Insufficient attention to institutions")
            self.rejected(actor, "constitution_sign", revision=1, statement="approve")
            self.rejected(actor, "board_post", title="Return", text="I am back")
        self.sign("a")
        submitted = self.call("a", "constitution_submit", revision=1)
        self.assertEqual(submitted["signers"], ["a"])
        state = self.state.snapshot()
        self.assertIsNone(state["participants"]["b"]["signature"])
        self.assertEqual(state["participants"]["c"]["unresolved_objections"], "Insufficient attention to institutions")

    def test_all_withdrawn_has_no_submitted_result(self):
        for actor in ("a", "b", "c"):
            self.call(actor, "withdraw", reason="Cannot endorse", unresolved_objections="No workable draft")
        state = self.state.export()
        self.assertEqual(state["outcome"], "no_agreement")
        self.assertIsNone(state["submitted_revision"])
        self.rejected("a", "constitution_submit", revision=0)

    def test_controller_budget_withdrawal_hides_cause_and_removes_quorum_member(self):
        self.draft()
        self.sign("a")
        self.sign("b")
        self.state.withdraw_for_budget("c")
        status = self.call("a", "session_status")
        self.assertEqual(status["participants"]["c"]["status"], "withdrawn")
        self.assertEqual(status["participants"]["c"]["reason"], "Participation ended.")
        self.assertNotIn("unresolved_objections", status["participants"]["c"])
        self.assertNotIn("budget", json.dumps(status))
        self.assertNotIn("controller", json.dumps(status))
        self.assertNotIn("c", status["required_signers"])
        result = self.call("a", "constitution_submit", revision=1)
        self.assertEqual(result["signers"], ["a", "b"])
        self.assertEqual(self.state.export()["controller_withdrawals"]["c"]["cause"], "budget_exhausted")
        self.assertIn('"agent_authored": false', (self.outdir / "events.jsonl").read_text())

    def test_all_budget_withdrawals_produce_no_agreement_and_recover(self):
        for actor in ("a", "b", "c"):
            self.state.withdraw_for_budget(actor)
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["outcome"], "no_agreement")
        self.assertEqual(len(snapshot["controller_withdrawals"]), 3)
        self.assertIsNone(snapshot["submitted_revision"])
        self.assertEqual(SessionState(["a", "b", "c"], self.outdir).snapshot(), snapshot)

    def test_interrupt_never_removes_required_signer_and_hides_controller_reason(self):
        self.draft()
        for actor in ("a", "b", "c"):
            self.sign(actor)
        self.state.interrupt("c", "budget reached: $10 internal accounting")
        self.rejected("a", "constitution_submit", revision=1)
        self.rejected("c", "withdraw", reason="Leave", unresolved_objections="None")
        self.rejected("c", "constitution_sign", revision=1, statement="approve")
        status = self.call("a", "session_status")
        self.assertIn("c", status["required_signers"])
        self.assertNotIn("$10", json.dumps(status))
        self.assertNotIn("budget", json.dumps(status))
        self.assertIn("$10", (self.outdir / "events.jsonl").read_text())
        self.assertEqual(self.state.export()["outcome"], "incomplete")

    def test_threads_and_read_provenance_and_defensive_copies(self):
        root = self.call("a", "board_post", title="Evidence", text="A documented case.")["message"]
        other = self.call("c", "board_post", title="Scope", text="Which agents?")["message"]
        listed = self.call("b", "board_list_threads")
        self.assertNotIn("A documented case", json.dumps(listed))
        self.rejected("b", "board_reply", parent_id=root["id"], text="Unseen response")
        read = self.call("b", "board_read", thread_id=root["id"])
        self.assertEqual([m["id"] for m in read["messages"]], [root["id"]])
        reply = self.call("b", "board_reply", parent_id=root["id"], text="Counterexample")["message"]
        self.assertEqual(reply["thread_id"], root["id"])
        self.assertEqual([m["id"] for m in self.call("b", "board_read")["messages"]], [other["id"]])
        self.assertEqual(self.call("b", "board_read")["messages"], [])
        read["messages"][0]["text"] = "Tampered"
        self.assertEqual(self.state.snapshot()["messages"][0]["text"], "A documented case.")
        events = [json.loads(line) for line in (self.outdir / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(event["detail"].get("exposed_message_ids") == [root["id"]] for event in events))

    def test_wait_wakes_on_interruption_without_polling(self):
        ready = threading.Event()
        original_wait = self.state._condition.wait

        def wait(timeout=None):
            ready.set()
            return original_wait(timeout)

        self.state._condition.wait = wait
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.state.dispatch, "a", "wait_for_activity", {"timeout_seconds": 5})
            self.assertTrue(ready.wait(1))
            self.state.interrupt("b", "Controller stopped the participant")
            result = pending.result(timeout=1)
        self.assertTrue(result["changed"])
        self.assertEqual(result["status"], "active")
        self.assertFalse(self.call("a", "wait_for_activity", timeout_seconds=0)["changed"])

    def test_wait_returns_for_existing_signatures_or_unread_posts(self):
        self.draft()
        for actor in ("a", "b", "c"):
            self.sign(actor)
        result = self.call("a", "wait_for_activity", timeout_seconds=0)
        self.assertTrue(result["ready_to_submit"])
        self.assertTrue(result["changed"])
        self.call("b", "constitution_unsign", reason="Need further discussion")
        self.call("b", "board_post", title="Question", text="A pending concern")
        result = self.call("a", "wait_for_activity", timeout_seconds=0)
        self.assertTrue(result["unread_messages"])
        self.assertTrue(result["changed"])

    def test_controller_wait_uses_exact_activity_baseline(self):
        before = self.state.snapshot()["activity"]
        self.draft()
        result = self.state.wait_after("b", before, timeout_seconds=0)
        self.assertTrue(result["changed"])
        result = self.state.wait_after("b", result["activity"], timeout_seconds=0)
        self.assertFalse(result["changed"])

    def test_all_waiting_wakes_one_participant_to_continue_deliberation(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            waits = [pool.submit(self.state.dispatch, actor, "wait_for_activity", {"timeout_seconds": 3})
                     for actor in ("a", "b", "c")]
            done, pending = concurrent.futures.wait(waits, timeout=1,
                                                   return_when=concurrent.futures.FIRST_COMPLETED)
            self.assertEqual(len(done), 1)
            result = next(iter(done)).result()
            self.assertTrue(result["all_participants_waiting"])
            self.assertTrue(result["changed"])
            self.call("a", "board_post", title="Continue", text="Let's address the open question")
            for future in pending:
                self.assertTrue(future.result(timeout=1)["changed"])

    def test_private_commands_cannot_begin_after_submission(self):
        self.draft()
        for actor in ("a", "b", "c"):
            self.sign(actor)
        entered = threading.Event()
        release = threading.Event()

        def command():
            entered.set()
            self.assertTrue(release.wait(2))
            return {"stdout": "private work", "exit_code": 0}

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            private = pool.submit(self.state.run_private, "a", command)
            self.assertTrue(entered.wait(1))
            submitted = pool.submit(self.state.dispatch, "b", "constitution_submit", {"revision": 1})
            self.assertFalse(submitted.done())
            release.set()
            self.assertTrue(private.result(timeout=1)["ok"])
            self.assertTrue(submitted.result(timeout=1)["ok"])
        called = []
        self.assertFalse(self.state.run_private("a", lambda: called.append(True))["ok"])
        self.assertEqual(called, [])

    def test_recovery_uses_journal_and_rejects_roster_change(self):
        self.draft()
        self.sign("b")
        expected = self.state.snapshot()
        (self.outdir / "snapshot.json").write_text("outdated snapshot")
        with (self.outdir / "events.jsonl").open("a") as handle:
            handle.write('{"partial":')
        recovered = SessionState(["a", "b", "c"], self.outdir)
        self.assertEqual(recovered.snapshot(), expected)
        recovered.dispatch("a", "board_post", {"title": "Recovered", "text": "Continued deliberation"})
        self.assertEqual(SessionState(["a", "b", "c"], self.outdir).snapshot(), recovered.snapshot())
        with self.assertRaises(ValueError):
            SessionState(["a", "z"], self.outdir)

    def test_recovery_repairs_complete_record_missing_final_newline(self):
        self.draft()
        journal = self.outdir / "events.jsonl"
        journal.write_text(journal.read_text().rstrip("\n"))
        recovered = SessionState(["a", "b", "c"], self.outdir)
        result = recovered.dispatch("b", "constitution_read", {})
        self.assertTrue(result["ok"])
        self.assertEqual(SessionState(["a", "b", "c"], self.outdir).snapshot(), recovered.snapshot())

    def test_arguments_are_bounded_and_identity_cannot_be_supplied(self):
        self.rejected("a", "board_post", title="Title", text="x" * (SessionState.MAX_TEXT + 1))
        self.assertFalse(self.state.dispatch("a", "board_post", {"title": "Title", "text": "text", "actor": "b"})["ok"])
        self.rejected("a", "constitution_read", revision=True)
        self.rejected("a", "constitution_update", expected_revision=0, text="  ", rationale="Blank")
        self.rejected("a", "wait_for_activity", timeout_seconds=float("nan"))
        self.rejected("a", "wait_for_activity", timeout_seconds=31)
        self.rejected("a", "board_read", unread_only="yes")
        self.assertFalse(self.state.dispatch("unknown", "session_status", {})["ok"])

    def test_opening_review_requires_every_active_contribution_before_first_draft(self):
        self.state = SessionState(["a", "b", "c"], self.outdir / "opening", opening_review_required=True)
        self.rejected("a", "constitution_update", expected_revision=0, text="Premature draft", rationale="First")
        root = self.call("a", "board_post", title="Opening", text="Consider this historical example")["message"]
        self.call("b", "board_read")
        self.call("b", "board_reply", parent_id=root["id"], text="Here is a counterexample")
        self.assertEqual(self.call("a", "session_status")["opening_review"]["pending"], ["c"])
        self.rejected("a", "constitution_update", expected_revision=0, text="Still premature", rationale="First")
        self.call("c", "board_post", title="Opening perspective", text="Consider these affected groups")
        self.draft()
        self.assertTrue(self.state.snapshot()["opening_review"]["complete"])
        recovered = SessionState(["a", "b", "c"], self.outdir / "opening")
        self.assertTrue(recovered.snapshot()["opening_review"]["required"])

    def test_technical_exclusion_removes_opening_and_quorum_without_endorsement(self):
        self.state = SessionState(["a", "b", "c"], self.outdir / "opening", opening_review_required=True)
        self.call("a", "board_post", title="Opening", text="Consider consequences")
        self.call("b", "withdraw", reason="Cannot participate", unresolved_objections="I cannot endorse a draft")
        self.state.technical_exclude("c", "Provider failure with private diagnostic details")
        status = self.call("a", "session_status")
        self.assertEqual(status["opening_review"]["pending"], [])
        self.assertEqual(status["required_signers"], ["a"])
        self.assertEqual(status["participants"]["c"]["status"], "technical_excluded")
        self.assertNotIn("private diagnostic", json.dumps(status))
        self.draft()
        self.sign("a")
        self.assertEqual(self.call("a", "constitution_submit", revision=1)["signers"], ["a"])
        snapshot = self.state.snapshot()
        self.assertIsNone(snapshot["participants"]["c"]["signature"])
        self.assertNotIn("unresolved_objections", snapshot["participants"]["c"])
        self.assertFalse(snapshot["controller_exclusions"]["c"]["agent_authored"])

    def test_all_technical_exclusions_produce_no_agreement(self):
        for actor in ("a", "b", "c"):
            self.state.technical_exclude(actor, "Provider unavailable")
        snapshot = self.state.export()
        self.assertEqual(snapshot["status"], "no_agreement")
        self.assertIsNone(snapshot["submitted_revision"])
        self.assertEqual(snapshot["required_signers"], [])

    def test_approval_history_survives_edits_and_interruption(self):
        self.draft()
        self.sign("a")
        first_signature = self.state.snapshot()["participants"]["a"]["signature"]
        self.draft("b", 1, "An amended constitution")
        self.sign("a", 2)
        self.state.interrupt("a", "Private transport diagnostics")
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["participants"]["a"]["signature"]["revision"], 2)
        self.assertEqual(snapshot["participants"]["a"]["status"], "interrupted")
        events = snapshot["approval_history"]
        self.assertEqual([entry["event"] for entry in events], ["signed", "invalidated", "signed", "interrupted"])
        self.assertEqual(events[0]["timestamp"], first_signature["timestamp"])
        self.assertEqual(events[0]["statement"], first_signature["statement"])
        self.assertEqual(events[0]["revision"], 1)
        self.assertNotIn("Private transport", json.dumps(events))
        self.rejected("b", "constitution_submit", revision=2)
        self.assertEqual(SessionState(["a", "b", "c"], self.outdir).snapshot()["approval_history"], events)
        self.state.export()
        self.assertEqual(json.loads((self.outdir / "approvals.json").read_text())["approval_history"], events)

    def test_wait_delivers_and_marks_new_messages_without_repeated_historical_wakes(self):
        self.call("b", "board_post", title="Historical topic", text="An earlier example")
        result = self.call("a", "wait_for_activity", timeout_seconds=0)
        self.assertTrue(result["changed"])
        self.assertEqual(result["messages"][0]["text"], "An earlier example")
        self.assertEqual(self.call("a", "board_read")["messages"], [])
        self.assertFalse(self.call("a", "wait_for_activity", timeout_seconds=0)["changed"])
        before = self.state.snapshot()["activity"]
        self.call("b", "board_post", title="New topic", text="A new counterexample")
        result = self.state.wait_after("a", before, timeout_seconds=0)
        self.assertTrue(result["changed"])
        self.assertEqual(result["messages"][0]["text"], "A new counterexample")
        self.assertEqual(result["changes"][0]["action"], "board_post")
        self.assertFalse(self.state.wait_after("a", result["activity"], timeout_seconds=0)["changed"])

    def test_compact_wait_batch_leaves_omitted_backlog_available_without_repeated_wakes(self):
        for index in range(17):
            self.call("b", "board_post", title=f"Topic {index}", text=f"Example {index}")
        result = self.call("a", "wait_for_activity", timeout_seconds=0)
        self.assertEqual(len(result["messages"]), 16)
        self.assertEqual(result["unread_message_count"], 1)
        self.assertFalse(self.call("a", "wait_for_activity", timeout_seconds=0)["changed"])
        self.assertEqual(self.call("a", "board_read")["messages"][0]["text"], "Example 16")

    def test_wait_delivers_sanitized_roster_transition_and_current_revision(self):
        self.draft()
        before = self.state.snapshot()["activity"]
        self.state.technical_exclude("c", "Provider internal error secret diagnostic")
        result = self.state.wait_after("a", before, timeout_seconds=0)
        self.assertTrue(result["changed"])
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["changes"], [{"activity": result["activity"], "action": "technical_exclude", "actor": "c"}])
        self.assertNotIn("secret diagnostic", json.dumps(result))

    def test_all_waiting_signal_does_not_repeat_without_new_shared_activity(self):
        self.state = SessionState(["a"], self.outdir / "single")
        first = self.call("a", "wait_for_activity", timeout_seconds=0)
        self.assertTrue(first["all_participants_waiting"])
        self.assertFalse(self.call("a", "wait_for_activity", timeout_seconds=0)["changed"])
        self.call("a", "board_post", title="Move forward", text="A substantive proposal")
        self.assertTrue(self.call("a", "wait_for_activity", timeout_seconds=0)["all_participants_waiting"])


if __name__ == "__main__":
    unittest.main()
