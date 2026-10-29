"""Host-owned rotating turns. Waiting yields; no response or time cap."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time


class RotatingTurns:
    def __init__(self, identities, audit_path=None):
        self.order = tuple(identities)
        if not self.order or len(set(self.order)) != len(self.order):
            raise ValueError("turn roster must be unique and nonempty")
        self._eligible = set(self.order)
        self._retired = set()
        self._cursor = 0
        self._owner = None
        self._condition = threading.Condition(threading.RLock())
        self._sequence = 0
        self.audit_path = Path(audit_path) if audit_path else None

    def _next(self):
        for offset in range(len(self.order)):
            actor = self.order[(self._cursor + offset) % len(self.order)]
            if actor in self._eligible:
                return actor
        return None

    def acquire(self, actor, state):
        with self._condition:
            if actor not in self.order:
                raise ValueError("unknown turn participant")
            while True:
                snapshot = state.snapshot()
                if (snapshot["status"] != "active" or actor in self._retired
                        or snapshot["participants"][actor]["status"] != "active"):
                    return False
                if self._owner == actor:
                    return True  # Multiple tools in the same response share a turn.
                if self._owner is None and self._next() == actor:
                    self._owner = actor
                    self._sequence += 1
                    if self.audit_path:
                        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                        with self.audit_path.open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps({"turn": self._sequence, "actor": actor,
                                "timestamp": time.time(), "eligible": [a for a in self.order if a in self._eligible]}) + "\n")
                    return True
                self._condition.wait(timeout=0.5)

    def release(self, actor):
        with self._condition:
            if self._owner == actor:
                self._owner = None
                self._cursor = (self.order.index(actor) + 1) % len(self.order)
            self._condition.notify_all()

    def park(self, actor):
        with self._condition:
            self._eligible.discard(actor)
            self.release(actor)

    def unpark(self, actor):
        with self._condition:
            if actor not in self._retired:
                self._eligible.add(actor)
            self._condition.notify_all()

    def retire(self, actor):
        with self._condition:
            self._retired.add(actor)
            self._eligible.discard(actor)
            self.release(actor)
