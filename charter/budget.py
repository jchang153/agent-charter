"""Small loopback OpenRouter proxy with a conservative dollar budget.

The caller supplies the OpenRouter API key in memory. This module never reads
environment variables or credential files and does not log request bodies or
headers. It is intentionally limited to the chat-completions route and three
allow-listed models.
"""

from __future__ import annotations

import json
import http.client
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from .tools import RESEARCH_TOOL_PARAMETERS


MIB = 1024 * 1024
MAX_REQUEST_BYTES = 2 * MIB
MAX_RESPONSE_BYTES = 5 * MIB
DEFAULT_OUTPUT_TOKENS = 8192
MAX_OUTPUT_TOKENS = 65536
UPSTREAM_TIMEOUT_SECONDS = 600
CONTINGENCY_USD = 0.50
UPSTREAM_DEFAULT = "https://openrouter.ai/api/v1"

# Input/output USD per million tokens.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-5": (1.25, 10.0),
    "openai/o3": (2.0, 8.0),
    "anthropic/claude-sonnet-4": (3.0, 15.0),
}


class BudgetGateway:
    """An ephemeral loopback proxy that reserves and charges a shared budget.

    ``upstream_url`` is an optional testing seam; production callers should use
    the default OpenRouter endpoint. ``outdir`` receives an atomically replaced
    sanitized ``usage.jsonl`` plus a small accounting state file.
    """

    def __init__(
        self,
        key: str,
        outdir: str | os.PathLike[str],
        limit: float | None = 20.0,
        *,
        upstream_url: str = UPSTREAM_DEFAULT,
        agent_limits: dict[str, float | None] | None = None,
        max_tokens: int | None = None,
        deadline_monotonic: float | None = None,
        max_in_flight: int = 1,
        allow_reserved_uncertainty: bool = False,
        recorded_spending_only: bool = False,
    ) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("An OpenRouter key must be supplied in memory")
        if limit is not None and (not math.isfinite(float(limit)) or float(limit) <= 0):
            raise ValueError("limit must be a positive finite dollar amount")
        if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0):
            raise ValueError("max_tokens must be a positive integer")
        if agent_limits is not None:
            if not agent_limits or any(not isinstance(a, str) or not a or (v is not None and (isinstance(v, bool) or not math.isfinite(float(v)) or float(v) <= 0)) for a, v in agent_limits.items()):
                raise ValueError("agent_limits must map agent IDs to positive dollar limits")
        if deadline_monotonic is not None and not math.isfinite(deadline_monotonic):
            raise ValueError("deadline must be finite")
        if isinstance(max_in_flight, bool) or not isinstance(max_in_flight, int) or max_in_flight < 1:
            raise ValueError("max_in_flight must be a positive integer")
        self._recorded_spending_only = recorded_spending_only
        self._agent_limits = dict(agent_limits or {})
        self._max_tokens = max_tokens
        self._deadline = deadline_monotonic
        self._reserved_agents: dict[str, str] = {}
        self._reserved_tokens: dict[str, int] = {}
        self._stop_reason: str | None = None
        self._agent_stops: dict[str, str] = {}
        self._quarantined_agents: dict[str, str] = {}
        self.__key = key
        self._limit = None if limit is None else float(limit)
        self._upstream_url = upstream_url.rstrip("/")
        self._outdir = Path(outdir)
        self._outdir.mkdir(parents=True, exist_ok=True)
        self._ledger_path = self._outdir / "usage.jsonl"
        self._state_path = self._outdir / "accounting.json"
        self._lock = threading.RLock()
        # Caps concurrent upstream calls. Budget safety does not depend on it:
        # every in-flight request already holds a conservative reservation.
        self._in_flight = threading.BoundedSemaphore(max_in_flight)
        self._spent = 0.0
        self._reserved: dict[str, float] = {}
        self._accounting_uncertain = False
        self._allow_reserved_uncertainty = allow_reserved_uncertainty
        self._recoverable_uncertainty = False
        self._entries: list[dict[str, Any]] = []
        self._load_state()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        """Start the proxy on an ephemeral IPv4 loopback port and return base URL."""
        with self._lock:
            if self._server is not None:
                return self.base_url
            owner = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, _format: str, *args: Any) -> None:
                    # BaseHTTPRequestHandler's default access log can contain
                    # request targets; intentionally suppress all access logs.
                    return

                def do_POST(self) -> None:
                    owner._handle_post(self)

                def do_GET(self) -> None:
                    owner._handle_get(self)

                def do_HEAD(self) -> None:
                    self.send_error(405)

                def do_PUT(self) -> None:
                    self.send_error(405)

                def do_PATCH(self) -> None:
                    self.send_error(405)

                def do_DELETE(self) -> None:
                    self.send_error(405)

            self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self._server.daemon_threads = True
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="impossible-budget-gateway",
                daemon=True,
            )
            self._thread.start()
            return self.base_url

    @property
    def base_url(self) -> str:
        server = self._server
        if server is None:
            raise RuntimeError("Gateway has not been started")
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/api/v1"

    def stop(self) -> None:
        """Stop the listener and wait briefly for its serving thread."""
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)

    def snapshot(self) -> dict[str, Any]:
        """Return sanitized accounting state (never includes credentials)."""
        with self._lock:
            return {
                "limit_usd": self._limit,
                "admission_policy": "recorded_spending" if self._recorded_spending_only else "reserved_commitment",
                "spent_usd": round(self._spent, 10),
                "reserved_usd": round(sum(self._reserved.values()), 10),
                "accounting_uncertain": self._accounting_uncertain,
                "quarantined_agents": self._quarantined_agents,
                "safe_to_continue": not self._accounting_uncertain or (
                    self._allow_reserved_uncertainty and self._recoverable_uncertainty),
                "requests_charged": len(self._entries),
                "spent_tokens": sum(e["tokens"] for e in self._entries),
                "reserved_tokens": sum(self._reserved_tokens.values()),
                "max_tokens": self._max_tokens,
                "stop_reason": self._stop_reason,
                "agents": {a: {
                    "limit_usd": limit,
                    "spent_usd": sum(e["cost"] for e in self._entries if e.get("agent") == a),
                    "reserved_usd": sum(v for k, v in self._reserved.items() if self._reserved_agents.get(k) == a),
                    "spent_tokens": sum(e["tokens"] for e in self._entries if e.get("agent") == a),
                    "stop_reason": self._agent_stops.get(a),
                } for a, limit in self._agent_limits.items()},
            }

    def _load_state(self) -> None:
        if self._ledger_path.exists():
            try:
                lines = self._ledger_path.read_text(encoding="utf-8").splitlines()
                for line in lines:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    cost = item.get("cost")
                    if not isinstance(cost, (int, float)) or isinstance(cost, bool):
                        raise ValueError("invalid stored cost")
                    cost = float(cost)
                    if not math.isfinite(cost) or cost < 0:
                        raise ValueError("invalid stored cost")
                    tokens = item["tokens"]
                    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                        raise ValueError("invalid stored tokens")
                    if self._agent_limits and item.get("agent") not in self._agent_limits:
                        raise ValueError("unknown stored agent")
                    self._spent += cost
                    # Keep only expected non-secret usage fields in memory.
                    self._entries.append(
                        {
                            "model": str(item["model"]),
                            "agent": str(item.get("agent", "")),
                            "id": str(item["id"]),
                            "cost": cost,
                            "tokens": tokens,
                            "time": str(item["time"]),
                        }
                    )
            except Exception:
                self._accounting_uncertain = True
        if self._state_path.exists():
            try:
                ledger_valid = not self._accounting_uncertain
                state = json.loads(self._state_path.read_text(encoding="utf-8"))
                if not math.isclose(float(state.get('spent_usd', 0)), self._spent, abs_tol=1e-9):
                    raise ValueError('Stored spending disagrees with usage ledger')
                if state.get("accounting_uncertain") is True:
                    self._accounting_uncertain = True
                reservations = state.get("reservations", {})
                if not isinstance(reservations, dict):
                    raise ValueError("invalid stored reservations")
                for request_id, amount in reservations.items():
                    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                        raise ValueError("invalid stored reservation")
                    amount = float(amount)
                    if not math.isfinite(amount) or amount < 0:
                        raise ValueError("invalid stored reservation")
                    self._reserved[str(request_id)] = amount
                self._reserved_agents = {str(k): str(v) for k, v in state.get("reservation_agents", {}).items()}
                for k, v in state.get("reservation_tokens", {}).items():
                    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                        raise ValueError("invalid token reservation")
                    self._reserved_tokens[str(k)] = v
                self._stop_reason = state.get("stop_reason")
                self._agent_stops = dict(state.get("agent_stops", {}))
                self._quarantined_agents = dict(state.get("quarantined_agents", {}))
                saved_reserved = state.get("reserved_usd", 0.0)
                if isinstance(saved_reserved, bool) or not isinstance(saved_reserved, (int, float)):
                    raise ValueError("invalid stored reserved total")
                if not math.isfinite(float(saved_reserved)) or float(saved_reserved) < 0:
                    raise ValueError("invalid stored reserved total")
                if self._reserved or float(saved_reserved) > 0:
                    self._accounting_uncertain = True
                self._recoverable_uncertainty = (
                    ledger_valid and state.get('recoverable_uncertainty') is True
                    and bool(self._reserved)
                    and set(self._reserved) == set(self._reserved_agents) == set(self._reserved_tokens)
                    and math.isclose(float(saved_reserved), sum(self._reserved.values()), abs_tol=1e-9))
            except Exception:
                self._accounting_uncertain = True
                self._recoverable_uncertainty = False

    def _atomic_write(self, path: Path, content: str) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self._outdir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _persist_locked(self) -> None:
        ledger = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in self._entries)
        self._atomic_write(self._ledger_path, ledger)
        state = {
            "spent_usd": round(self._spent, 10),
            "reserved_usd": round(sum(self._reserved.values()), 10),
            "reservations": self._reserved,
            "reservation_agents": self._reserved_agents,
            "reservation_tokens": self._reserved_tokens,
            "stop_reason": self._stop_reason,
            "agent_stops": self._agent_stops,
            "accounting_uncertain": self._accounting_uncertain,
                "quarantined_agents": self._quarantined_agents,
            "recoverable_uncertainty": self._recoverable_uncertainty,
        }
        self._atomic_write(self._state_path, json.dumps(state, separators=(",", ":")) + "\n")

    @staticmethod
    def _path_kind(path: str) -> tuple[str | None, str]:
        parsed = urllib.parse.urlsplit(path)
        clean = parsed.path.rstrip("/")
        if clean.endswith("/api/v1/chat/completions"):
            return "chat", parsed.query
        if clean.endswith("/chat/completions"):
            return "chat", parsed.query
        if clean.endswith("/api/v1/models") or clean == "/models":
            return "models", parsed.query
        return None, parsed.query

    @staticmethod
    def _reply(handler: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        try:
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
        body = json.dumps({"error": {"message": message, "type": "budget_gateway_error"}}).encode()
        self._reply(handler, status, body)

    def _read_request(self, handler: BaseHTTPRequestHandler) -> bytes | None:
        if handler.headers.get("Transfer-Encoding"):
            self._error(handler, 411, "content length required")
            return None
        try:
            length = int(handler.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            self._error(handler, 411, "content length required")
            return None
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._error(handler, 413, "request body exceeds gateway limit")
            return None
        body = handler.rfile.read(length)
        if len(body) != length:
            self._error(handler, 400, "incomplete request body")
            return None
        return body

    def _lookup_generation_usage(self, generation_id, model):
        """Read existing generation billing only; never repeat inference."""
        if not isinstance(generation_id, str) or not generation_id.startswith("gen-") or len(generation_id) > 256:
            return None
        for attempt in range(3):
            try:
                url = self._upstream_url + "/generation?" + urllib.parse.urlencode({"id": generation_id})
                request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.__key})
                with urllib.request.urlopen(request, timeout=10) as response:
                    raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise ValueError("metadata too large")
                data = json.loads(raw)["data"]
                if data.get("id") != generation_id or data.get("model") != model:
                    raise ValueError("generation mismatch")
                cost = data["total_cost"]
                counts = [data["tokens_prompt"], data["tokens_completion"]]
                if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
                    raise ValueError("invalid cost")
                if any(type(v) is not int or v < 0 for v in counts):
                    raise ValueError("invalid counts")
                return {"cost": cost, "total_tokens": sum(counts)}
            except Exception:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        return None

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        kind, query = self._path_kind(handler.path)
        if kind != "models":
            self._error(handler, 404, "route not available")
            return
        url = f"{self._upstream_url}/models" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    self._error(handler, 502, "upstream response exceeds gateway limit")
                    return
                self._reply(handler, response.status, body)
        except urllib.error.HTTPError as exc:
            self._error(handler, 502, f"model catalog request failed ({exc.code})")
        except Exception:
            self._error(handler, 502, "model catalog request failed")

    def _estimate(self, model: str, body_len: int, output_tokens: int) -> float:
        input_price, output_price = MODEL_PRICES[model]
        # Byte count is deliberately used as a high-side token proxy. The extra
        # 10k covers chat framing/system overhead. Sonnet's input reserve also
        # includes the requested cache-write uplift.
        prompt_tokens = body_len + 10_000
        if model == "anthropic/claude-sonnet-4" and prompt_tokens > 200_000:
            input_price *= 2
            output_price *= 2
        if model == "anthropic/claude-sonnet-4":
            input_price *= 1.25
        return (prompt_tokens * input_price + output_tokens * output_price) / 1_000_000

    def _request_reservation(self, model, payload, body_len, output_tokens):
        """Reserve retrieval fees, returned context, and extra server-tool rounds."""
        names = set()
        for tool in payload.get("tools", []):
            kind = tool.get("type", "")
            if kind.startswith("openrouter:"):
                if kind not in RESEARCH_TOOL_PARAMETERS or tool.get("parameters") != RESEARCH_TOOL_PARAMETERS[kind] or kind in names:
                    raise ValueError("unsupported or unbounded server-tool configuration")
                names.add(kind)
        rounds = 1 + len(names)
        added_tokens = 0
        fees = 0.0
        if "openrouter:web_search" in names:
            # Public Exa fast-search list price is $0.007; reserve a margin.
            added_tokens += 5 * 2000 * 4
            fees += 0.02
        if "openrouter:web_fetch" in names:
            # Direct fetch is free; allow a generous byte/token context margin.
            added_tokens += 8000 * 4
        return (rounds * self._estimate(model, body_len + added_tokens, output_tokens) + fees,
                rounds * (body_len + added_tokens + 10_000 + output_tokens))

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        kind, _query = self._path_kind(handler.path)
        if kind != "chat":
            self._error(handler, 404, "route not available")
            return
        raw = self._read_request(handler)
        if raw is None:
            return
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("request must be an object")
        except Exception:
            self._error(handler, 400, "invalid JSON request")
            return
        model = payload.get("model")
        if not isinstance(model, str) or model not in MODEL_PRICES:
            self._error(handler, 400, "model is not allow-listed")
            return
        if payload.get("stream") is True:
            self._error(handler, 400, "streaming is disabled by the budget gateway")
            return
        token_fields = [payload[k] for k in ("max_tokens", "max_completion_tokens") if k in payload]
        output_tokens = DEFAULT_OUTPUT_TOKENS
        for value in token_fields:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > MAX_OUTPUT_TOKENS:
                self._error(handler, 400, f"output token limit must be between 1 and {MAX_OUTPUT_TOKENS}")
                return
        # If neither cap is supplied, reserve the full allowed 8192 token output.
        if token_fields:
            output_tokens = max(token_fields)
        else:
            # A budget reservation alone does not constrain provider output.
            # Materialize the cap in the forwarded request as well.
            payload["max_tokens"] = DEFAULT_OUTPUT_TOKENS
            raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if len(raw) > MAX_REQUEST_BYTES:
                self._error(handler, 413, "request body exceeds gateway limit")
                return
        # OpenAI reserves the function name python. Alias only on the wire,
        # restoring names in responses so the unchanged upstream tool executes.
        alias_python = model.startswith("openai/")
        if alias_python:
            for tool in payload.get("tools", []):
                fn = tool.get("function", {})
                fn["strict"] = False  # Upstream editor schema has optional fields.
                if fn.get("name") == "python": fn["name"] = "execute_python"
            for message in payload.get("messages", []):
                if message.get("name") == "python": message["name"] = "execute_python"
                for call in message.get("tool_calls", []):
                    fn = call.get("function", {})
                    if fn.get("name") == "python": fn["name"] = "execute_python"
            choice = payload.get("tool_choice")
            if isinstance(choice, dict) and choice.get("function", {}).get("name") == "python":
                choice["function"]["name"] = "execute_python"
            raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        agent_id = handler.headers.get("x-agent-id", "")
        if self._agent_limits and agent_id not in self._agent_limits:
            self._error(handler, 400, "known x-agent-id is required")
            return
        try:
            reservation, token_reservation = self._request_reservation(model, payload, len(raw), output_tokens)
        except (ValueError, TypeError, AttributeError):
            self._error(handler, 400, "server-tool configuration must be bounded and approved")
            return
        request_id = f"request-{threading.get_ident()}-{datetime.now(timezone.utc).timestamp()}"

        # Admission and reservation happen atomically under the lock, so
        # concurrent requests cannot oversubscribe the budget. The upstream
        # call itself runs without the lock; its reservation stays counted
        # until the response is settled.
        with self._in_flight:
            with self._lock:
                if self._deadline is not None and time.monotonic() >= self._deadline:
                    self._stop_reason = "deadline"
                    self._persist_locked()
                    self._error(handler, 402, "episode deadline reached")
                    return
                if self._stop_reason:
                    self._error(handler, 402, "remaining budget exhausted: " + self._stop_reason)
                    return
                if agent_id in self._quarantined_agents:
                    self._error(handler, 503, "participant accounting quarantined")
                    return
                if agent_id in self._agent_stops:
                    self._error(handler, 402, "agent budget exhausted")
                    return
                if self._accounting_uncertain and not (
                    self._allow_reserved_uncertainty and self._recoverable_uncertainty):
                    self._error(handler, 503, "accounting is uncertain; dispatch disabled")
                    return
                if self._agent_limits:
                    agent_committed = sum(e["cost"] for e in self._entries if e.get("agent") == agent_id) + sum(v for k, v in self._reserved.items() if self._reserved_agents.get(k) == agent_id) + reservation
                    agent_spent = sum(e["cost"] for e in self._entries if e.get("agent") == agent_id)
                    reached = agent_spent >= self._agent_limits[agent_id] if self._recorded_spending_only and self._agent_limits[agent_id] is not None else self._agent_limits[agent_id] is not None and agent_committed > self._agent_limits[agent_id]
                    if reached:
                        self._agent_stops[agent_id] = "dollars"
                        self._persist_locked()
                        self._error(handler, 402, "agent budget exhausted")
                        return
                if self._max_tokens is not None and sum(e["tokens"] for e in self._entries) + sum(self._reserved_tokens.values()) + token_reservation > self._max_tokens:
                    self._stop_reason = "tokens"
                    self._persist_locked()
                    self._error(handler, 402, "remaining budget exhausted: tokens")
                    return
                committed = self._spent + sum(self._reserved.values()) + reservation + CONTINGENCY_USD
                if self._limit is not None and (self._spent >= self._limit if self._recorded_spending_only else committed > self._limit):
                    self._stop_reason = "dollars"
                    self._persist_locked()
                    self._error(handler, 402, "request exceeds remaining budget")
                    return
                self._reserved[request_id] = reservation
                self._reserved_agents[request_id] = agent_id
                self._reserved_tokens[request_id] = token_reservation
                try:
                    # Persist before dispatch so a process crash while the upstream
                    # call is in flight leaves a conservative reservation behind.
                    self._persist_locked()
                except Exception:
                    self._mark_uncertain_locked(request_id)
                    self._error(handler, 503, "could not persist budget reservation; dispatch disabled")
                    return
            url = f"{self._upstream_url}/chat/completions"
            forwarded_headers = {
                "Authorization": f"Bearer {self.__key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            session_id = handler.headers.get("x-session-id")
            if session_id is not None:
                forwarded_headers["x-session-id"] = session_id
            upstream_request = urllib.request.Request(
                url,
                data=raw,
                method="POST",
                headers=forwarded_headers,
            )
            failure: BaseException | None = None
            try:
                with urllib.request.urlopen(upstream_request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
                    upstream_body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(upstream_body) > MAX_RESPONSE_BYTES:
                        raise RuntimeError("response_too_large")
                    if response.status < 200 or response.status >= 300:
                        raise RuntimeError(f"upstream_status_{response.status}")
                    upstream_status = response.status
                response_json = None
                try:
                    response_json = json.loads(upstream_body)
                    usage = response_json["usage"]
                    cost = usage["cost"]
                    tokens = usage["total_tokens"]
                    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
                        raise ValueError("invalid cost")
                    cost = float(cost)
                    if not math.isfinite(cost) or cost < 0:
                        raise ValueError("invalid cost")
                    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                        raise ValueError("invalid token count")
                except Exception as exc:
                    recovered = self._lookup_generation_usage(
                        response_json.get("id") if isinstance(response_json, dict) else None, model)
                    if recovered is None:
                        raise RuntimeError("usage_missing_or_invalid") from exc
                    cost = float(recovered["cost"])
                    tokens = recovered["total_tokens"]
                    response_json["usage"] = recovered
                    upstream_body = json.dumps(response_json).encode()
                    self._atomic_write(self._outdir / (request_id + ".reconciled.json"),
                        json.dumps({"request_id": request_id, "generation_id": response_json.get("id"),
                            "agent": agent_id, "cost": cost, "source": "generation_metadata"}))
            except BaseException as exc:  # noqa: BLE001 - settled below under the lock
                failure = exc

        if failure is None:
            if alias_python:
                for choice in response_json.get("choices", []):
                    for call in choice.get("message", {}).get("tool_calls", []):
                        fn = call.get("function", {})
                        if fn.get("name") == "execute_python": fn["name"] = "python"
                upstream_body = json.dumps(response_json, ensure_ascii=False).encode("utf-8")
            upstream_id = response_json.get("id", "")
            if not isinstance(upstream_id, str):
                upstream_id = ""
            row = {
                "model": model,
                "agent": agent_id,
                "id": upstream_id[:200],
                "cost": cost,
                "tokens": tokens,
                "time": datetime.now(timezone.utc).isoformat(),
            }
            with self._lock:
                if not self._recorded_spending_only and (cost > reservation or tokens > token_reservation):
                    self._stop_reason = "reservation_exceeded"
                self._spent += cost
                self._entries.append(row)
                self._reserved.pop(request_id, None)
                self._reserved_agents.pop(request_id, None)
                self._reserved_tokens.pop(request_id, None)
                try:
                    self._persist_locked()
                except Exception:
                    self._accounting_uncertain = True
                    self._recoverable_uncertainty = False
                    self._error(handler, 503, 'could not persist usage; dispatch disabled')
                    return
            self._reply(handler, upstream_status, upstream_body)
            return

        exc = failure
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail=json.loads(exc.read(8192)).get('error',{})
                message=json.dumps(detail)[:7500].replace(self.__key,'[redacted]')
            except Exception: message='unavailable'
            with self._lock:
                self._mark_uncertain_locked(request_id, recoverable=exc.code == 429 or exc.code >= 500)
                try:
                    self._atomic_write(self._outdir/'last-error.json',json.dumps({'type':'HTTPError','status':exc.code,'message':message,'model':model}))
                except Exception:
                    self._mark_uncertain_locked(request_id)
            status = 502 if exc.code >= 500 else exc.code
            self._error(handler, status, f"upstream request failed ({exc.code}); accounting uncertain")
            return
        with self._lock:
            isolated_usage_failure = (str(exc) == "usage_missing_or_invalid"
                and self._allow_reserved_uncertainty and request_id in self._reserved)
            if isolated_usage_failure:
                self._quarantined_agents[agent_id] = "usage_missing_or_invalid"
                # Retain only billing provenance, never request headers or credentials.
                try:
                    metadata = {"request_id": request_id, "agent": agent_id, "model": model,
                        "generation_id": response_json.get("id") if isinstance(response_json, dict) else None,
                        "reservation_usd": self._reserved[request_id], "reason": "usage_missing_or_invalid"}
                    self._atomic_write(self._outdir / (request_id + ".pending.json"), json.dumps(metadata))
                except Exception:
                    isolated_usage_failure = False
            self._mark_uncertain_locked(request_id, recoverable=isolated_usage_failure or isinstance(exc, (
                TimeoutError, urllib.error.URLError, ConnectionError,
                http.client.IncompleteRead, http.client.RemoteDisconnected)))
            try:
                self._atomic_write(self._outdir/'last-error.json',json.dumps({'type':type(exc).__name__,'reason_type':type(getattr(exc,'reason',None)).__name__,'message':str(exc)[:500].replace(self.__key,'[redacted]'),'model':model}))
            except Exception:
                self._mark_uncertain_locked(request_id)
        if str(exc).startswith("upstream_status_"):
            code = str(exc).rsplit("_", 1)[-1]
            self._error(handler, 502, f"upstream request failed ({code}); accounting uncertain")
        elif str(exc) == "response_too_large":
            self._error(handler, 502, "upstream response too large; accounting uncertain")
        elif str(exc) == "usage_missing_or_invalid":
            self._error(handler, 502, "upstream usage missing or invalid; accounting uncertain")
        else:
            self._error(handler, 502, "upstream request failed; accounting uncertain")
        if not isinstance(exc, Exception):
            raise exc

    def _mark_uncertain_locked(self, request_id: str, *, recoverable: bool = False) -> None:
        self._recoverable_uncertainty = bool(
            recoverable and request_id in self._reserved
            and (not self._accounting_uncertain or self._recoverable_uncertainty))
        self._accounting_uncertain = True
        # Keep this request's reservation permanently in the snapshot. It may
        # have incurred a charge even if no valid usage record reached us.
        try:
            self._persist_locked()
        except Exception:
            self._recoverable_uncertainty = False
            # Dispatch remains disabled in memory even if storage is unavailable.
            pass


__all__ = ["BudgetGateway", "MODEL_PRICES"]
