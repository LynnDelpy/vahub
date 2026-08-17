"""The one path every tool call takes: validate, gate, dispatch, audit.

The agent, the scheduler and a confirmed destructive action all call through
here, which is the only reason the policy gate is a boundary at all: a second
way to reach a module would be a second way to bypass it.

Two properties this file exists to guarantee.

`call` always returns a structured dict and never raises. A tool call is
attacker-influenced input from three directions at once (the user's words, the
model's choice, the module's answer), and an exception escaping into the agent
loop would end the turn rather than let the model report the failure.

A confirmed destructive call is executed with the arguments that were frozen
when the confirmation was requested, never with whatever the context holds when
the human says yes. Otherwise "unlock the door" could be confirmed and
"unlock every door" executed.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from . import metrics
from .logging import get_logger
from .mcpclient import McpError
from .supervisor import Module, State, Supervisor, extract_payload

if TYPE_CHECKING:
    from ..agent.policy import Gate
    from ..storage.store import Store
    from .bus import EventBus

log = get_logger("moduleapi")

DEFAULT_TIMEOUT_S = 10.0


class ModuleAPI:
    def __init__(
        self,
        supervisor: Supervisor,
        gate: Gate | None = None,
        store: Store | None = None,
        bus: EventBus | None = None,
        confirm_ttl_s: float = 60.0,
    ) -> None:
        self._sup = supervisor
        self._gate = gate
        self._store = store
        self._bus = bus
        self._confirm_ttl_s = confirm_ttl_s

    # --- public ------------------------------------------------------------
    async def call(
        self,
        module: str,
        tool: str,
        args: dict[str, Any] | None = None,
        principal: str = "agent",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        try:
            return await self._call(module, tool, args, principal, timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the call path must not raise into its caller
            log.error("moduleapi_internal_error", module=module, tool=tool, error=str(e))
            return {"ok": False, "error": "internal", "detail": str(e)}

    async def confirm(
        self,
        pending_id: str,
        subject: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Execute a call that was previously approved, using its frozen args.

        `subject` is whoever approved it (the authenticating proxy's header, if
        there is one). It is recorded as the acting principal in the audit log,
        so the row says who said yes, not just that something said yes."""
        try:
            return await self._confirm(pending_id, subject, timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("moduleapi_confirm_error", pending_id=pending_id, error=str(e))
            return {"ok": False, "error": "internal", "detail": str(e)}

    async def cancel(self, pending_id: str) -> dict[str, Any]:
        if self._store is None:
            return {"ok": False, "error": "no_store"}
        pending = await self._store.get_pending(pending_id)
        if pending is None:
            return {"ok": False, "error": "unknown_pending"}
        if pending.get("status") != "pending":
            return {"ok": False, "error": "not_pending", "detail": pending.get("status")}
        await self._store.set_pending_status(pending_id, "cancelled")
        return {"ok": True, "result": {"pending_id": pending_id, "status": "cancelled"}}

    # --- internals ---------------------------------------------------------
    async def _call(
        self,
        module: str,
        tool: str,
        args: dict[str, Any] | None,
        principal: str,
        timeout_s: float,
    ) -> dict[str, Any]:
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return {"ok": False, "error": "bad_args", "detail": "args must be an object"}
        if not isinstance(tool, str) or not tool:
            return {"ok": False, "error": "bad_tool", "detail": "tool must be a name"}

        mod = self._sup.modules.get(module)
        if mod is None:
            return {"ok": False, "error": "unknown_module", "detail": module}
        if tool.startswith("__"):
            # Reserved surface (the health probe). It is the hub's, not the
            # model's, and it is never in the catalog.
            return {"ok": False, "error": "reserved_tool", "detail": tool}
        if mod.state is not State.READY or mod.client is None:
            # An unavailable module answers immediately. Waiting for one that is
            # restarting would turn a module outage into a hung conversation.
            return {"ok": False, "error": "module_not_ready", "detail": mod.state.value}
        if not any(t.get("name") == tool for t in mod.tools):
            return {"ok": False, "error": "unknown_tool", "detail": tool}

        decision_name = "allow"
        if self._gate is not None:
            decision = self._gate.evaluate(principal, module, tool, args)
            outcome = getattr(decision, "outcome", "deny")
            reason = getattr(decision, "reason", "")
            metrics.POLICY_DECISIONS.labels(
                principal=principal, module=module, tool=tool, outcome=outcome
            ).inc()
            if outcome == "deny":
                await self._audit(principal, module, tool, args, "deny", "denied", None)
                return {"ok": False, "error": "policy_denied", "detail": reason}
            if outcome == "confirm":
                pending_id = await self._create_pending(principal, module, tool, args, reason)
                await self._audit(principal, module, tool, args, "confirm", "pending", None)
                return {
                    "ok": False,
                    "error": "confirmation_required",
                    "pending_id": pending_id,
                    "detail": reason,
                }
            decision_name = "allow"

        return await self._dispatch(mod, tool, args, timeout_s, principal, decision_name)

    async def _confirm(self, pending_id: str, subject: str | None, timeout_s: float) -> dict[str, Any]:
        if self._store is None:
            return {"ok": False, "error": "no_store"}
        pending = await self._store.get_pending(pending_id)
        if pending is None:
            return {"ok": False, "error": "unknown_pending"}
        if pending.get("status") != "pending":
            return {"ok": False, "error": "not_pending", "detail": pending.get("status")}
        if time.time() > float(pending.get("expires_at", 0)):
            # A confirmation that outlived its window is not a confirmation: the
            # world it was about has moved on.
            await self._store.set_pending_status(pending_id, "expired")
            return {"ok": False, "error": "expired"}

        module = str(pending.get("module", ""))
        tool = str(pending.get("tool", ""))
        try:
            args = json.loads(pending.get("args") or "{}")
        except ValueError:
            return {"ok": False, "error": "bad_pending", "detail": "stored arguments are unreadable"}
        if not isinstance(args, dict):
            return {"ok": False, "error": "bad_pending", "detail": "stored arguments are not an object"}

        mod = self._sup.modules.get(module)
        if mod is None or mod.state is not State.READY or mod.client is None:
            return {"ok": False, "error": "module_not_ready"}

        # Claim the pending call atomically. Two concurrent confirmations of the
        # same id both reach this point with status still 'pending'; only the one
        # that wins the compare-and-set dispatches, so a single human approval
        # cannot fire the frozen destructive call twice.
        if not await self._store.consume_pending(pending_id):
            return {"ok": False, "error": "not_pending", "detail": "already handled"}
        # The gate is not re-run here. These exact arguments already passed it;
        # re-evaluating them under the confirming human's principal would deny
        # the very flow the gate asked for.
        return await self._dispatch(mod, tool, args, timeout_s, subject or "user", "allow-confirmed")

    async def _dispatch(
        self,
        mod: Module,
        tool: str,
        args: dict[str, Any],
        timeout_s: float,
        principal: str,
        decision: str,
    ) -> dict[str, Any]:
        module = mod.name
        client = mod.client
        if client is None:
            # The caller checks readiness first, but a module can die in the
            # window between that check and here, and for a confirmed call the
            # pending row is already consumed. Audit the loss rather than
            # returning a bare error that leaves no record of what was approved.
            return await self._failure(
                principal,
                module,
                tool,
                args,
                decision,
                "module_not_ready",
                time.monotonic(),
                mod.state.value,
            )

        async with mod.lock:  # one in-flight call per module
            t0 = time.monotonic()
            try:
                raw = await client.call_tool(tool, args, timeout_s)
            except TimeoutError:
                return await self._failure(
                    principal, module, tool, args, decision, "timeout", t0, f"{timeout_s}s"
                )
            except McpError as e:
                return await self._failure(
                    principal, module, tool, args, decision, "mcp_error", t0, e.message
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return await self._failure(principal, module, tool, args, decision, "internal", t0, str(e))
            finally:
                elapsed = time.monotonic() - t0
                metrics.TOOL_LATENCY.labels(module=module, tool=tool).observe(elapsed)
                metrics.observe_stage(metrics.STAGE_TOOL, elapsed)

        if not isinstance(raw, dict):
            return await self._failure(
                principal, module, tool, args, decision, "bad_result", t0, "non-object result"
            )
        if raw.get("isError"):
            detail = extract_payload(raw)
            return await self._failure(principal, module, tool, args, decision, "tool_error", t0, detail)

        metrics.TOOL_CALLS.labels(module=module, tool=tool, result="ok").inc()
        await self._audit(principal, module, tool, args, decision, "ok", _ms(t0))
        self._publish_called(principal, module, tool, decision, "ok", _ms(t0))
        return {"ok": True, "result": _unwrap(extract_payload(raw))}

    async def _failure(
        self,
        principal: str,
        module: str,
        tool: str,
        args: dict[str, Any],
        decision: str,
        kind: str,
        t0: float,
        detail: Any,
    ) -> dict[str, Any]:
        metrics.TOOL_CALLS.labels(module=module, tool=tool, result=kind).inc()
        await self._audit(principal, module, tool, args, decision, kind, _ms(t0))
        self._publish_called(principal, module, tool, decision, kind, _ms(t0))
        return {"ok": False, "error": kind, "detail": detail}

    async def _create_pending(
        self, principal: str, module: str, tool: str, args: dict[str, Any], reason: str
    ) -> str:
        pending_id = uuid.uuid4().hex
        if self._store is not None:
            await self._store.create_pending(pending_id, principal, module, tool, args, self._confirm_ttl_s)
        if self._bus is not None:
            self._bus.publish(
                "policy.confirmation_required",
                {
                    "pending_id": pending_id,
                    "module": module,
                    "tool": tool,
                    # The frozen arguments, so the person approving sees exactly
                    # what will run. They already passed the gate (so they are
                    # bounded by its constraints); the page inserts them as text.
                    "args": args,
                    "principal": principal,
                    "reason": reason,
                    "ttl_s": self._confirm_ttl_s,
                },
            )
        return pending_id

    async def _audit(
        self,
        principal: str,
        module: str,
        tool: str,
        args: dict[str, Any],
        decision: str,
        result: str,
        duration_ms: float | None,
    ) -> None:
        if self._store is None:
            return
        redacted = _redact(args, self._redact_keys(module))
        try:
            await self._store.record_tool_call(
                principal, module, tool, redacted, decision, result, duration_ms
            )
        except Exception as e:  # a failing audit must not fail the call
            log.warning("audit_write_failed", module=module, tool=tool, error=str(e))

    def _publish_called(
        self,
        principal: str,
        module: str,
        tool: str,
        decision: str,
        result: str,
        duration_ms: float | None,
    ) -> None:
        if self._bus is None:
            return
        self._bus.publish(
            "tool.called",
            {
                "principal": principal,
                "module": module,
                "tool": tool,
                "decision": decision,
                "result": result,
                "duration_ms": duration_ms,
            },
        )

    def _redact_keys(self, module: str) -> list[str]:
        mod = self._sup.modules.get(module)
        return list(mod.manifest.audit.redact) if mod is not None else []


def _ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000, 2)


def _redact(args: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    if not keys:
        return args
    return {k: ("***" if k in keys else v) for k, v in args.items()}


def _unwrap(payload: Any) -> Any:
    """MCP servers built on FastMCP wrap a scalar return as {"result": value}.
    Peel that single envelope so callers see the value, not the envelope."""
    if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
        return payload["result"]
    return payload
