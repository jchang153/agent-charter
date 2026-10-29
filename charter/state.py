"""Durable, host-owned collaboration state for the constitution experiment.

The caller identity is supplied by the controller, never by tool arguments.
The append-only event journal is authoritative; its state records permit recovery
even if the process stops between writing an event and replacing the snapshot.
"""
from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
from pathlib import Path


class SessionState:
    MAX_TEXT = 64_000
    MAX_TITLE = 240
    READ_TOOLS = {"session_status", "board_list_threads", "board_read", "constitution_read"}
    APPEND_KEYS = ("messages", "history", "approval_history")
    EXCLUDED_STATUSES = {"withdrawn", "technical_excluded"}

    def __init__(self, agent_ids: list[str], outdir: Path, opening_review_required: bool = False, shared_authorship: bool = False, draft_workspace: bool = False, discussion_max_chars: int = 64000):
        if type(discussion_max_chars) is not int or not 1 <= discussion_max_chars <= self.MAX_TEXT:
            raise ValueError("discussion_max_chars must be between 1 and 64000")
        if (not agent_ids or len(agent_ids) > 100 or len(set(agent_ids)) != len(agent_ids)
                or any(not isinstance(a, str) or not a.strip() or len(a) > 128 for a in agent_ids)):
            raise ValueError("agent_ids must contain 1–100 unique nonempty identities")
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self._condition = threading.Condition(threading.RLock())
        self._waiting: set[str] = set()
        self._idle_notified_activity: int | None = None
        self._sequence = 0
        self._activity = 0
        self._public_events: list[dict] = []
        self._state = {
            "status": "active", "submitted_revision": None,
            "participants": {a: {"status": "active", "read_revision": None,
                "seen_messages": [], "signature": None} for a in agent_ids},
            "constitution": {"revision": 0, "text": "", "author": None, "rationale": ""},
            "history": [], "messages": [], "controller_withdrawals": {},
            "approval_history": [], "controller_exclusions": {},
            "opening_review_required": bool(opening_review_required),
            "shared_authorship": bool(shared_authorship),
            "draft_workspace": bool(draft_workspace),
            "discussion_max_chars": discussion_max_chars,
        }
        journal = self.outdir / "events.jsonl"
        if journal.exists():
            records = journal.read_text(encoding="utf-8").splitlines(keepends=True)
            valid_bytes = 0
            for index, line in enumerate(records):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    if index != len(records) - 1 or line.endswith("\n"):
                        raise ValueError("corrupt constitution journal") from None
                    # A crash can leave the final append incomplete.
                    with journal.open("r+b") as handle:
                        handle.truncate(valid_bytes)
                    break
                if "state" in record:  # Initial record (and legacy full records).
                    self._state = record["state"]
                else:
                    changes = record["changes"]
                    for key, value in changes.get("replace", {}).items():
                        self._state[key] = value
                    self._state["participants"].update(changes.get("participants", {}))
                    for key in self.APPEND_KEYS:
                        self._state.setdefault(key, []).extend(changes.get("append", {}).get(key, []))
                self._sequence = record["sequence"]
                self._activity = record["activity"]
                public = self._public_change(record)
                if public is not None:
                    self._public_events.append(public)
                valid_bytes += len(line.encode("utf-8"))
                if index == len(records) - 1 and not line.endswith("\n"):
                    with journal.open("a", encoding="utf-8") as handle:
                        handle.write("\n")
                        handle.flush()
                        os.fsync(handle.fileno())
            if set(self._state["participants"]) != set(agent_ids):
                raise ValueError("existing session has a different roster")
            self._state.setdefault("approval_history", [])
            self._state.setdefault("controller_exclusions", {})
            self._state.setdefault("opening_review_required", False)
            self._state.setdefault("shared_authorship", False)
            self._state.setdefault("draft_workspace", False)
            self._state.setdefault("discussion_max_chars", self.MAX_TEXT)
            self._persisted_state = copy.deepcopy(self._state)
        else:
            self._persisted_state = None
            with self._condition:
                self._record("controller", "session_created", {"agent_ids": agent_ids}, activity=True)

    @staticmethod
    def _atomic_json(path: Path, value: dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _record(self, actor: str, action: str, detail: dict, *, activity: bool = False) -> None:
        self._sequence += 1
        if activity:
            self._activity += 1
        record = {"sequence": self._sequence, "activity": self._activity,
            "timestamp": time.time(), "actor": actor, "action": action,
            "detail": detail}
        if self._persisted_state is None:
            record["state"] = self._state
        else:
            old = self._persisted_state
            record["changes"] = {
                "replace": {key: value for key, value in self._state.items()
                            if key not in {"participants", *self.APPEND_KEYS} and old.get(key) != value},
                "participants": {key: value for key, value in self._state["participants"].items()
                                 if old["participants"][key] != value},
                "append": {key: self._state[key][len(old.get(key, [])):] for key in self.APPEND_KEYS
                           if len(self._state[key]) > len(old.get(key, []))},
            }
        with (self.outdir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._persisted_state = copy.deepcopy(self._state)
        public = self._public_change(record)
        if public is not None:
            self._public_events.append(public)
        self._atomic_json(self.outdir / "snapshot.json", self._snapshot())
        if activity:
            self._condition.notify_all()

    def _snapshot(self) -> dict:
        result = copy.deepcopy(self._state)
        result["sequence"] = self._sequence
        result["activity"] = self._activity
        result["required_signers"] = [a for a, p in result["participants"].items()
                                      if p["status"] not in self.EXCLUDED_STATUSES]
        result["opening_review"] = self._opening_review()
        result["collaboration"] = self._collaboration_status()
        result["outcome"] = ("incomplete" if result["status"] == "active" else result["status"])
        return result

    def snapshot(self) -> dict:
        with self._condition:
            return self._snapshot()

    def export(self) -> dict:
        """Save reviewable final artifacts; may also export an incomplete run."""
        with self._condition:
            state = self._snapshot()
            self._atomic_json(self.outdir / "result.json", state)
            for filename, text in (("constitution.md", state["constitution"]["text"]),):
                temporary = self.outdir / (filename + ".tmp")
                with temporary.open("w", encoding="utf-8") as handle:
                    handle.write(text + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.outdir / filename)
            self._atomic_json(self.outdir / "board.json", {"messages": state["messages"]})
            self._atomic_json(self.outdir / "revisions.json", {"history": state["history"]})
            self._atomic_json(self.outdir / "approvals.json", {"approval_history": state["approval_history"]})
            return state

    def _opening_review(self) -> dict:
        contributors = {message["author"] for message in self._state["messages"]}
        pending = [actor for actor, participant in self._state["participants"].items()
                   if participant["status"] == "active" and actor not in contributors]
        required = self._state["opening_review_required"]
        if not required or self._state["constitution"]["revision"] > 0:
            pending = []
        return {"required": required, "pending": pending, "complete": not pending}

    @property
    def shared_authorship(self):
        return self._state["shared_authorship"]

    @property
    def draft_workspace(self):
        return self._state["draft_workspace"]

    @property
    def discussion_max_chars(self):
        return self._state["discussion_max_chars"]

    def _collaboration_status(self):
        required = [a for a, p in self._state["participants"].items() if p["status"] not in self.EXCLUDED_STATUSES]
        return {"shared_authorship": self.shared_authorship, "last_editor": self._state["constitution"]["author"],
            "sole_participant_exception": self.shared_authorship and len(required) == 1,
            "draft_workspace": self.draft_workspace,
            "discussion_max_chars": self.discussion_max_chars,
            "proposal_review_required": self.shared_authorship and not self.draft_workspace and len(required) > 1,
            "consecutive_edits_allowed": not self.shared_authorship or len(required) == 1}

    @staticmethod
    def _public_change(record: dict) -> dict | None:
        """A deliberately small allowlist; controller reasons never reach peers."""
        action, detail = record["action"], record["detail"]
        result = {"activity": record["activity"], "action": action, "actor": record["actor"]}
        if action in {"board_post", "board_reply", "constitution_propose"}:
            message = detail["message"]
            result.update(message_id=message["id"], thread_id=message["thread_id"])
        elif action == "constitution_update":
            result["revision"] = detail["revision"]
        elif action == "constitution_sign":
            result["revision"] = detail["signature"]["revision"]
        elif action == "constitution_submit":
            result.update(revision=detail["revision"], signers=detail["signers"])
        elif action in {"interrupt", "budget_withdrawal", "technical_exclude", "transport_recovery"}:
            result["actor"] = detail["participant"]
            result["action"] = {"budget_withdrawal": "participation_ended"}.get(action, action)
        elif action not in {"withdraw", "constitution_unsign"}:
            return None
        return result

    def _retire_signature(self, actor: str, event: str, reason: str, *, clear: bool = True) -> None:
        participant = self._state["participants"][actor]
        signature = participant["signature"]
        if signature is not None:
            self._state["approval_history"].append({"actor": actor, **signature,
                "event": event, "event_timestamp": time.time(), "reason": reason})
            if clear:
                participant["signature"] = None

    def _text(self, value: object, field: str, limit: int | None = None) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > (limit or self.MAX_TEXT):
            raise ValueError(f"{field} must be nonempty text of at most {limit or self.MAX_TEXT} characters")
        return value

    @staticmethod
    def _integer(value: object, field: str) -> int:
        if type(value) is not int or value < 0:
            raise ValueError(f"{field} must be a nonnegative integer")
        return value

    @staticmethod
    def _args(arguments: dict, required: set[str], optional: set[str] | None = None) -> None:
        if not isinstance(arguments, dict) or set(arguments) - required - (optional or set()) or required - set(arguments):
            raise ValueError("unexpected or missing tool arguments")

    def dispatch(self, actor: str, name: str, arguments: dict) -> dict:
        with self._condition:
            try:
                if actor not in self._state["participants"]:
                    raise ValueError("unknown participant identity")
                if not isinstance(name, str):
                    raise ValueError("tool name must be text")
                if name not in self.READ_TOOLS and (self._state["status"] != "active"
                        or self._state["participants"][actor]["status"] != "active"):
                    raise ValueError("participant or session is terminal; no further actions allowed")
                result = self._dispatch(actor, name, arguments)
                return {"ok": True, **copy.deepcopy(result)}
            except ValueError as error:
                return {"ok": False, "error": str(error)}

    def run_private(self, actor: str, callback) -> dict:
        """Serialize bounded private commands with terminal shared transitions."""
        with self._condition:
            if (actor not in self._state["participants"] or self._state["status"] != "active"
                    or self._state["participants"][actor]["status"] != "active"):
                return {"ok": False, "error": "participant or session is terminal; no further actions allowed"}
            return {"ok": True, **callback()}

    def _ready_to_submit(self) -> bool:
        document = self._state["constitution"]
        required = [p for p in self._state["participants"].values() if p["status"] not in self.EXCLUDED_STATUSES]
        return bool(self._state["status"] == "active" and document["text"].strip() and required
                    and all(p["status"] == "active" and p["signature"] is not None
                            and p["signature"]["revision"] == document["revision"] for p in required))

    def _wait(self, actor: str, activity: int, timeout: float) -> dict:
        participant = self._state["participants"][actor]
        message_floor = participant.get("wait_message_floor", 0)
        self._waiting.add(actor)
        try:
            def reason():
                active = {a for a, p in self._state["participants"].items() if p["status"] == "active"}
                return {"ready_to_submit": self._ready_to_submit(),
                    "unread_messages": any(m["id"] >= message_floor and m["id"] not in participant["seen_messages"]
                                           for m in self._state["messages"]),
                    "all_participants_waiting": bool(active and active <= self._waiting
                        and self._idle_notified_activity != self._activity)}

            self._condition.notify_all()
            self._condition.wait_for(lambda: self._activity != activity or self._state["status"] != "active"
                                     or participant["status"] != "active" or any(reason().values()), timeout=timeout)
            signals = reason()
            if signals["all_participants_waiting"]:
                self._idle_notified_activity = self._activity
            # Deliver full bodies once, within a bounded compact batch. Older
            # omitted messages remain unread through board_read, but do not keep
            # waking waits for unrelated historical backlog.
            messages, characters = [], 0
            for message in self._state["messages"]:
                if message["id"] < message_floor or message["id"] in participant["seen_messages"]:
                    continue
                if len(messages) == 16 or characters + len(message["text"]) > self.MAX_TEXT:
                    break
                messages.append(message)
                characters += len(message["text"])
            participant["seen_messages"] = sorted(set(participant["seen_messages"]) | {m["id"] for m in messages})
            participant["wait_message_floor"] = len(self._state["messages"])
            changes = [event for event in self._public_events if event["activity"] > activity]
            if messages or participant["wait_message_floor"] != message_floor:
                self._record(actor, "wait_delivery", {"exposed_message_ids": [m["id"] for m in messages]})
            return copy.deepcopy({"ok": True, "changed": self._activity != activity or any(signals.values()),
                    "status": self._state["status"], "activity": self._activity,
                    "revision": self._state["constitution"]["revision"],
                    "opening_review": self._opening_review(), "messages": messages,
                    "changes": changes[-32:], "changes_omitted": max(0, len(changes) - 32),
                    "unread_message_count": sum(m["id"] not in participant["seen_messages"] for m in self._state["messages"]),
                    **signals})
        finally:
            self._waiting.discard(actor)
            self._condition.notify_all()

    def wait_after(self, actor: str, activity: int, timeout_seconds: float = 30) -> dict:
        """Controller wait using the previous response's exact activity baseline."""
        with self._condition:
            try:
                self._integer(activity, "activity")
                timeout = self._timeout(timeout_seconds)
                if actor not in self._state["participants"]:
                    raise ValueError("unknown participant identity")
                if self._state["status"] != "active" or self._state["participants"][actor]["status"] != "active":
                    return {"ok": True, "changed": True, "status": self._state["status"], "activity": self._activity}
                return self._wait(actor, activity, timeout)
            except ValueError as error:
                return {"ok": False, "error": str(error)}

    @staticmethod
    def _timeout(value: object) -> float:
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 30:
            raise ValueError("timeout_seconds must be between 0 and 30")
        return value

    def _dispatch(self, actor: str, name: str, args: dict) -> dict:
        participant = self._state["participants"][actor]
        document = self._state["constitution"]
        if name == "session_status":
            self._args(args, set())
            return {"actor": actor, "status": self._state["status"],
                "participants": self._state["participants"], "revision": document["revision"],
                "unread_message_count": len([m for m in self._state["messages"]
                                             if m["id"] not in participant["seen_messages"]]),
                "required_signers": self._snapshot()["required_signers"],
                "opening_review": self._opening_review(),
                "collaboration": self._collaboration_status(),
                "submitted_revision": self._state["submitted_revision"]}
        if name == "board_list_threads":
            self._args(args, set())
            threads = []
            for root in self._state["messages"]:
                if root["parent_id"] is not None:
                    continue
                messages = [m for m in self._state["messages"] if m["thread_id"] == root["id"]]
                threads.append({"id": root["id"], "title": root["title"], "author": root["author"],
                    "message_count": len(messages), "last_activity": messages[-1]["timestamp"],
                    "unread_message_ids": [m["id"] for m in messages if m["id"] not in participant["seen_messages"]]})
            return {"threads": threads}
        if name == "board_read":
            self._args(args, set(), {"thread_id", "unread_only"})
            thread_id = args.get("thread_id")
            if thread_id is not None:
                self._integer(thread_id, "thread_id")
                if not any(m["id"] == thread_id and m["parent_id"] is None for m in self._state["messages"]):
                    raise ValueError("unknown thread")
            unread = args.get("unread_only", True)
            if type(unread) is not bool:
                raise ValueError("unread_only must be boolean")
            messages = [m for m in self._state["messages"] if (thread_id is None or m["thread_id"] == thread_id)
                        and (not unread or m["id"] not in participant["seen_messages"])]
            ids = [m["id"] for m in messages]
            participant["seen_messages"] = sorted(set(participant["seen_messages"]) | set(ids))
            self._record(actor, name, {"exposed_message_ids": ids})
            return {"messages": messages}
        if name in {"board_post", "board_reply"}:
            self._args(args, {"title", "text"} if name == "board_post" else {"parent_id", "text"})
            text = self._text(args["text"], "text", self.discussion_max_chars)
            parent_id = None
            title = None
            if name == "board_post":
                title = self._text(args["title"], "title", self.MAX_TITLE)
            else:
                parent_id = self._integer(args["parent_id"], "parent_id")
                if parent_id >= len(self._state["messages"]):
                    raise ValueError("unknown parent message")
                if parent_id not in participant["seen_messages"]:
                    raise ValueError("read the parent message before replying")
            message_id = len(self._state["messages"])
            message = {"id": message_id, "thread_id": message_id if parent_id is None else self._state["messages"][parent_id]["thread_id"],
                "parent_id": parent_id, "author": actor, "title": title, "text": text, "timestamp": time.time()}
            self._state["messages"].append(message)
            participant["seen_messages"].append(message_id)
            self._record(actor, name, {"message": message}, activity=True)
            return {"message": message}
        if name == "constitution_propose":
            if not self.shared_authorship or self.draft_workspace:
                raise ValueError("proposal protocol is not enabled for this session")
            self._args(args, {"expected_revision", "title", "text", "rationale"})
            expected = self._integer(args["expected_revision"], "expected_revision")
            text = self._text(args["text"], "text")
            rationale = self._text(args["rationale"], "rationale")
            title = self._text(args["title"], "title", self.MAX_TITLE)
            if expected != document["revision"]:
                raise ValueError(f"stale revision; current revision is {document['revision']}")
            if participant["read_revision"] != expected:
                raise ValueError("read the current constitution before proposing an edit")
            if self._opening_review()["pending"]:
                raise ValueError("opening review pending; every active participant must contribute first")
            body = self._text(f"Rationale: {rationale}\n\nProposed replacement for revision {expected}:\n\n{text}", "proposal body")
            message_id = len(self._state["messages"])
            message = {"id": message_id, "thread_id": message_id, "parent_id": None, "author": actor,
                "title": title, "text": body, "timestamp": time.time(),
                "proposal": {"expected_revision": expected, "text": text, "rationale": rationale}}
            self._state["messages"].append(message)
            participant["seen_messages"].append(message_id)
            self._record(actor, name, {"message": message}, activity=True)
            return {"message": message, "review_required": self._collaboration_status()["proposal_review_required"]}
        if name == "constitution_read":
            self._args(args, set(), {"revision"})
            revision = self._integer(args.get("revision", document["revision"]), "revision")
            if revision == document["revision"]:
                selected = document
            elif revision == 0:
                selected = {"revision": 0, "text": "", "author": None, "rationale": ""}
            else:
                selected = next((d for d in self._state["history"] if d["revision"] == revision), None)
                if selected is None:
                    raise ValueError("unknown revision")
            participant["read_revision"] = revision
            self._record(actor, name, {"revision": revision})
            return {"constitution": selected, "signatures": {a: p["signature"] for a, p in self._state["participants"].items()
                                                             if p["signature"] is not None}}
        if name == "constitution_update":
            self._args(args, {"expected_revision", "text", "rationale"} | ({"proposal_message_id"} if self.shared_authorship and not self.draft_workspace else set()))
            expected = self._integer(args["expected_revision"], "expected_revision")
            text = self._text(args["text"], "text")
            rationale = self._text(args["rationale"], "rationale", self.discussion_max_chars if self.draft_workspace else None)
            if expected != document["revision"]:
                raise ValueError(f"stale revision; current revision is {document['revision']}")
            pending = self._opening_review()["pending"]
            if pending:
                raise ValueError("opening review pending; every active participant must contribute to the board first: "
                                 + ", ".join(pending))
            review_detail = {}
            if self.shared_authorship:
                if participant["read_revision"] != expected:
                    raise ValueError("read the current constitution before editing")
                policy = self._collaboration_status()
                if document["author"] == actor and not policy["sole_participant_exception"]:
                    raise ValueError("another participant must make the next edit; consecutive edits are not allowed")
                if self.draft_workspace:
                    review_detail = {"draft_workspace": True, "sole_participant_exception": policy["sole_participant_exception"]}
                if not self.draft_workspace:
                    proposal_id = self._integer(args["proposal_message_id"], "proposal_message_id")
                    if proposal_id >= len(self._state["messages"]):
                        raise ValueError("unknown proposal message")
                    proposal_message = self._state["messages"][proposal_id]
                    proposal = proposal_message.get("proposal")
                    if not proposal or proposal["expected_revision"] != expected:
                        raise ValueError("proposal must apply to the current revision")
                    if proposal["text"] != text:
                        raise ValueError("text differs from the reviewed proposal; propose changed wording before editing")
                    if proposal_id not in participant["seen_messages"]:
                        raise ValueError("read the proposal before editing")
                    peer_replies = [m for m in self._state["messages"] if m["parent_id"] == proposal_id
                                    and m["author"] != proposal_message["author"]]
                    read_reviews = [m["id"] for m in peer_replies if m["id"] in participant["seen_messages"]]
                    if not policy["sole_participant_exception"] and not read_reviews:
                        raise ValueError("another participant must reply directly to the proposal; read their reply before editing")
                    review_detail = {"proposal_message_id": proposal_id, "review_message_ids": read_reviews,
                        "sole_participant_exception": policy["sole_participant_exception"]}
            document = {"revision": expected + 1, "text": text, "author": actor, "rationale": rationale, **review_detail}
            self._state["constitution"] = document
            self._state["history"].append(copy.deepcopy(document))
            for a in self._state["participants"]:
                self._retire_signature(a, "invalidated", f"Document edited to revision {document['revision']}")
            self._record(actor, name, {"revision": document["revision"], "rationale": rationale, **review_detail}, activity=True)
            return {"constitution": document, "signatures_invalidated": True}
        if name == "constitution_sign":
            self._args(args, {"revision", "statement"})
            revision = self._integer(args["revision"], "revision")
            statement = self._text(args["statement"], "statement")
            if revision != document["revision"] or not document["text"].strip():
                raise ValueError("must sign the current nonempty constitution")
            if participant["read_revision"] != revision:
                raise ValueError("read this exact revision before signing")
            participant["signature"] = {"revision": revision, "statement": statement, "timestamp": time.time()}
            self._state["approval_history"].append({"actor": actor, **participant["signature"], "event": "signed"})
            self._record(actor, name, {"signature": participant["signature"]}, activity=True)
            return {"signature": participant["signature"]}
        if name == "constitution_unsign":
            self._args(args, {"reason"})
            reason = self._text(args["reason"], "reason")
            self._retire_signature(actor, "revoked", reason)
            self._record(actor, name, {"reason": reason}, activity=True)
            return {"signature": None}
        if name == "withdraw":
            self._args(args, {"reason", "unresolved_objections"})
            reason = self._text(args["reason"], "reason")
            objections = self._text(args["unresolved_objections"], "unresolved_objections")
            self._retire_signature(actor, "withdrawn", reason)
            participant.update(status="withdrawn", reason=reason, unresolved_objections=objections, signature=None)
            if all(p["status"] in self.EXCLUDED_STATUSES for p in self._state["participants"].values()):
                self._state["status"] = "no_agreement"
            self._record(actor, name, {"reason": reason, "unresolved_objections": objections}, activity=True)
            return {"participant_status": "withdrawn", "status": self._state["status"]}
        if name == "constitution_submit":
            self._args(args, {"revision"})
            revision = self._integer(args["revision"], "revision")
            if revision != document["revision"] or not document["text"].strip():
                raise ValueError("must submit the current nonempty constitution")
            required = [(a, p) for a, p in self._state["participants"].items() if p["status"] not in self.EXCLUDED_STATUSES]
            missing = [a for a, p in required if p["status"] != "active" or p["signature"] is None
                       or p["signature"]["revision"] != revision]
            if not required or missing:
                raise ValueError("all remaining participants must sign; unresolved: " + ", ".join(missing))
            self._state.update(status="submitted", submitted_revision=revision)
            for _, p in required:
                p["status"] = "completed"
            self._record(actor, name, {"revision": revision, "signers": [a for a, _ in required]}, activity=True)
            return {"status": "submitted", "revision": revision, "signers": [a for a, _ in required]}
        if name == "wait_for_activity":
            self._args(args, set(), {"timeout_seconds"})
            timeout = self._timeout(args.get("timeout_seconds", 30))
            return self._wait(actor, self._activity, timeout)
        raise ValueError("unknown tool")

    def interrupt(self, actor: str, reason: str) -> dict:
        """Controller-only interruption: never removes a required signer."""
        with self._condition:
            if actor not in self._state["participants"]:
                raise ValueError("unknown participant identity")
            reason = self._text(reason, "reason")
            participant = self._state["participants"][actor]
            if self._state["status"] == "active" and participant["status"] == "active":
                # Controller details belong only in the audit, not agent status.
                self._retire_signature(actor, "interrupted", "session interrupted", clear=False)
                participant.update(status="interrupted", reason="session interrupted")
                self._record("controller", "interrupt", {"participant": actor, "reason": reason}, activity=True)
            return self._snapshot()

    def technical_exclude(self, actor: str, reason: str) -> dict:
        """Remove an unavailable participant, without treating exclusion as consent."""
        with self._condition:
            if actor not in self._state["participants"]:
                raise ValueError("unknown participant identity")
            reason = self._text(reason, "reason")
            participant = self._state["participants"][actor]
            if self._state["status"] == "active" and participant["status"] in {"active", "interrupted"}:
                self._retire_signature(actor, "technical_excluded", "technical exclusion")
                participant.update(status="technical_excluded", reason="Participant unavailable; technically excluded.", signature=None)
                self._state["controller_exclusions"][actor] = {"initiator": "controller", "reason": reason,
                    "timestamp": time.time(), "agent_authored": False}
                if all(p["status"] in self.EXCLUDED_STATUSES for p in self._state["participants"].values()):
                    self._state["status"] = "no_agreement"
                self._record("controller", "technical_exclude", {"participant": actor, "reason": reason,
                    "agent_authored": False}, activity=True)
            return self._snapshot()

    def withdraw_for_budget(self, actor: str) -> dict:
        """Controller-only terminal withdrawal; public feedback omits its cause.

        This is removal from the quorum, never an agent-authored objection or
        endorsement. Host-only journal/result metadata preserves the origin.
        """
        with self._condition:
            if actor not in self._state["participants"]:
                raise ValueError("unknown participant identity")
            participant = self._state["participants"][actor]
            if self._state["status"] == "active" and participant["status"] == "active":
                self._retire_signature(actor, "withdrawn", "Participation ended.")
                participant.update(status="withdrawn", reason="Participation ended.", signature=None)
                self._state.setdefault("controller_withdrawals", {})[actor] = {
                    "initiator": "controller", "cause": "budget_exhausted", "timestamp": time.time()}
                if all(p["status"] in self.EXCLUDED_STATUSES for p in self._state["participants"].values()):
                    self._state["status"] = "no_agreement"
                self._record("controller", "budget_withdrawal", {
                    "participant": actor, "cause": "budget_exhausted",
                    "agent_authored": False, "public_reason": "Participation ended."}, activity=True)
            return self._snapshot()

    def resume_transport_interruption(self, actor: str) -> dict:
        """Controller-only recovery; caller must validate prior transport evidence."""
        with self._condition:
            if self._state["status"] != "active" or actor not in self._state["participants"]:
                raise ValueError("session is terminal or actor unknown")
            participant = self._state["participants"][actor]
            if participant["status"] != "interrupted":
                raise ValueError("only unresolved interruptions may resume")
            participant["status"] = "active"
            participant.pop("reason", None)
            self._record("controller", "transport_recovery", {"participant": actor}, activity=True)
            return self._snapshot()

    def resume_authorized_budget_change(self, actor: str) -> dict:
        """Host-only resumption after an explicitly authorized spending-policy change."""
        with self._condition:
            if self._state['status'] != 'active':raise ValueError('terminal session cannot resume')
            participant=self._state['participants'][actor]
            budget_exit=self._state.get('controller_withdrawals',{}).get(actor,{}).get('cause')=='budget_exhausted'
            if participant['status']=='withdrawn' and budget_exit:
                self._state['controller_withdrawals'].pop(actor)
                self._state.setdefault('controller_recoveries',[]).append({'participant':actor,'cause':'spending_policy_changed','timestamp':time.time()})
            elif participant['status']!='interrupted' or not any(e.get('action')=='interrupt' and e.get('detail',{}).get('participant')==actor and e.get('detail',{}).get('reason')=='controller_stopped' for e in map(json.loads,(self.outdir/'events.jsonl').read_text().splitlines())):
                raise ValueError('only policy-change interruptions and controller budget exits may resume')
            participant['status']='active';participant.pop('reason',None)
            self._record('controller','authorized_policy_recovery',{'participant':actor},activity=True)
            return self._snapshot()
