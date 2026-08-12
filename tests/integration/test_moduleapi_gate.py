"""The whole call path: validate, gate, dispatch, audit.

Every caller (the agent, the scheduler, the dev endpoint, a confirmation from
the UI) goes through ModuleAPI, so this is where the guarantees have to hold
rather than in any one of them. The three that matter: a denied call never
reaches the module, a destructive call runs with the arguments that were
approved and no others, and whatever happens ends up in the audit log.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from vahub.agent.policy import Gate
from vahub.config.models import Config, PolicyConfig
from vahub.core.moduleapi import ModuleAPI

pytestmark = pytest.mark.integration

POLICY: dict[str, Any] = {
    "default": "deny",
    "confirm_ttl_s": 60.0,
    "principals": {
        "agent": {"confirm": ["destructive"], "deny": []},
        "scheduler": {"confirm": [], "deny": ["fake.secretive"]},
        "user": {"confirm": [], "deny": []},
    },
    "rules": {
        "fake.echo": {"class": "read", "constraints": {"text": {"max_len": 20}}},
        "fake.add": {"class": "read", "constraints": {"a": {"range": [0, 100]}, "b": {"range": [0, 100]}}},
        "fake.sleep": {"class": "read", "constraints": {"seconds": {"range": [0, 10]}}},
        "fake.boom": {"class": "read"},
        "fake.stats": {"class": "read"},
        "fake.nondict": {"class": "read"},
        "fake.instructions": {"class": "read"},
        "fake.secretive": {"class": "destructive", "constraints": {"secret": {"max_len": 64}}},
    },
}


@pytest.fixture
def policy() -> PolicyConfig:
    return PolicyConfig.model_validate(POLICY)


@pytest.fixture
def gate(construct, policy: PolicyConfig) -> Gate:
    return construct(Gate, policy=policy, config=Config(policy=policy))


@pytest.fixture
def api(construct, supervisor, gate, store, bus, policy) -> ModuleAPI:
    return construct(
        ModuleAPI,
        supervisor=supervisor,
        gate=gate,
        store=store,
        bus=bus,
        config=Config(policy=policy),
        confirm_ttl_s=policy.confirm_ttl_s,
    )


async def calls_served(api: ModuleAPI) -> int:
    result = await api.call(module="fake", tool="stats", principal="user")
    assert result["ok"] is True, result
    return int(result["result"]["calls"])


async def audit(store) -> list[dict]:
    return await store.recent_tool_calls(limit=100)


# --------------------------------------------------------------------------
# denial
# --------------------------------------------------------------------------
async def test_a_denied_call_never_reaches_the_module(write_manifest, ready, api, store) -> None:
    write_manifest()
    await ready()
    before = await calls_served(api)

    result = await api.call(module="fake", tool="env_names", principal="agent")

    assert result["ok"] is False
    assert result["error"] == "policy_denied"
    # One call happened between the two probes: the probe itself.
    assert await calls_served(api) == before + 1
    assert [r["decision"] for r in await audit(store) if r["tool"] == "env_names"] == ["deny"]


async def test_an_argument_outside_its_constraint_is_denied(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()
    result = await api.call(module="fake", tool="echo", args={"text": "x" * 50}, principal="agent")
    assert result["error"] == "policy_denied"
    assert "text" in result["detail"]


async def test_an_undeclared_argument_is_denied(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()
    result = await api.call(module="fake", tool="add", args={"a": 1, "b": 2, "c": 3}, principal="agent")
    assert result["error"] == "policy_denied"
    assert "c" in result["detail"]


async def test_the_principal_decides(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()
    args = {"secret": "hunter2"}
    assert (await api.call(module="fake", tool="secretive", args=args, principal="scheduler"))[
        "error"
    ] == "policy_denied"
    assert (await api.call(module="fake", tool="secretive", args=args, principal="user"))["ok"] is True


async def test_the_reserved_health_tool_is_not_callable(write_manifest, ready, api) -> None:
    # The module lists __health, so only the hub refusing it keeps the probe
    # separate from the tools the model may use.
    write_manifest()
    await ready()
    result = await api.call(module="fake", tool="__health", principal="agent")
    assert result["ok"] is False
    assert result["error"] == "reserved_tool"


async def test_unknown_module_and_unknown_tool_are_structured_errors(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()
    assert (await api.call(module="nope", tool="echo"))["error"] == "unknown_module"
    assert (await api.call(module="fake", tool="no_such_tool"))["error"] == "unknown_tool"


async def test_a_module_that_is_not_running_answers_immediately(
    write_manifest, ready, api, supervisor
) -> None:
    write_manifest()
    await ready()
    await supervisor.stop()

    started = time.monotonic()
    result = await api.call(module="fake", tool="echo", args={"text": "hi"}, principal="agent")

    assert result["error"] == "module_not_ready"
    assert time.monotonic() - started < 1.0, "a stopped module must not be waited on"


# --------------------------------------------------------------------------
# confirmation
# --------------------------------------------------------------------------
async def test_a_destructive_call_becomes_a_pending_confirmation(
    write_manifest, ready, api, store, bus, collect
) -> None:
    events = collect(bus, "policy.confirmation_required")
    write_manifest()
    await ready()
    before = await calls_served(api)

    result = await api.call(
        module="fake", tool="secretive", args={"secret": "hunter2"}, principal="agent"
    )

    assert result["ok"] is False
    assert result["error"] == "confirmation_required"
    assert result["pending_id"]
    assert await calls_served(api) == before + 1, "nothing may run before a human agrees"

    await asyncio.sleep(0.05)
    assert [e["pending_id"] for e in events] == [result["pending_id"]]
    pending = await store.list_pending()
    assert [p["id"] for p in pending] == [result["pending_id"]]
    assert pending[0]["tool"] == "secretive"


async def test_confirming_executes_the_arguments_that_were_approved(
    write_manifest, ready, api, store
) -> None:
    write_manifest()
    await ready()
    args = {"secret": "hunter2"}
    pending_id = (
        await api.call(module="fake", tool="secretive", args=args, principal="agent")
    )["pending_id"]

    # Whatever the model does to its own copy afterwards is irrelevant: the
    # frozen arguments are what was shown to the human.
    args["secret"] = "something-else-entirely"

    result = await api.confirm(pending_id, subject="lucia")

    assert result["ok"] is True
    assert result["result"]["received"] == {"secret": "hunter2"}
    assert (await store.get_pending(pending_id))["status"] == "confirmed"


async def test_a_confirmation_can_only_be_spent_once(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()
    pending_id = (
        await api.call(module="fake", tool="secretive", args={"secret": "a"}, principal="agent")
    )["pending_id"]

    assert (await api.confirm(pending_id))["ok"] is True
    second = await api.confirm(pending_id)
    assert second["ok"] is False
    assert second["error"] == "not_pending"


async def test_an_unknown_confirmation_is_refused(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()
    assert (await api.confirm("0" * 32))["error"] == "unknown_pending"


async def test_a_confirmation_expires(construct, write_manifest, ready, supervisor, gate, store, bus) -> None:
    # A prompt left on a screen overnight is not consent given now.
    short = construct(
        ModuleAPI, supervisor=supervisor, gate=gate, store=store, bus=bus, confirm_ttl_s=0.05
    )
    write_manifest()
    await ready()
    pending_id = (
        await short.call(module="fake", tool="secretive", args={"secret": "a"}, principal="agent")
    )["pending_id"]

    await asyncio.sleep(0.2)
    result = await short.confirm(pending_id)

    assert result["error"] == "expired"
    assert (await store.get_pending(pending_id))["status"] == "expired"


async def test_confirmation_records_who_confirmed(write_manifest, ready, api, store) -> None:
    write_manifest()
    await ready()
    pending_id = (
        await api.call(module="fake", tool="secretive", args={"secret": "a"}, principal="agent")
    )["pending_id"]
    await api.confirm(pending_id, subject="lucia")

    executed = [r for r in await audit(store) if r["tool"] == "secretive" and r["result"] == "ok"]
    assert executed and executed[0]["principal"] == "lucia"


# --------------------------------------------------------------------------
# dispatch and untrusted output
# --------------------------------------------------------------------------
async def test_an_allowed_call_returns_the_modules_result(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()
    result = await api.call(module="fake", tool="add", args={"a": 20, "b": 22}, principal="agent")
    assert result == {"ok": True, "result": {"sum": 42}}


async def test_a_tool_error_is_reported_as_such(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()
    result = await api.call(module="fake", tool="boom", principal="agent")
    assert result["ok"] is False
    assert result["error"] == "tool_error"


async def test_a_result_that_is_not_an_object_is_rejected(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()
    result = await api.call(module="fake", tool="nondict", principal="agent")
    assert result["ok"] is False
    assert result["error"] == "bad_result"


async def test_a_timeout_is_not_handed_to_the_next_caller(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()

    timed_out = await api.call(
        module="fake", tool="sleep", args={"seconds": 0.6}, timeout_s=0.15, principal="agent"
    )
    assert timed_out["error"] == "timeout"

    # The module answers the abandoned request eventually. That answer must not
    # become the answer to this one.
    echoed = await api.call(module="fake", tool="echo", args={"text": "hallway"}, timeout_s=5)
    assert echoed["result"]["args"] == {"text": "hallway"}


async def test_calls_to_one_module_are_serialised(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()
    started = time.monotonic()

    results = await asyncio.gather(
        api.call(module="fake", tool="sleep", args={"seconds": 0.3}, timeout_s=5),
        api.call(module="fake", tool="sleep", args={"seconds": 0.3}, timeout_s=5),
    )

    assert all(r["ok"] for r in results)
    assert time.monotonic() - started >= 0.55, "a module handles one call at a time"


async def test_module_output_is_returned_as_data(write_manifest, ready, api) -> None:
    write_manifest()
    await ready()
    result = await api.call(module="fake", tool="instructions", principal="agent")
    # It comes back as a plain string payload. Nothing parses it, nothing acts
    # on it; the caller decides what to do with text.
    assert result["ok"] is True
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in result["result"]


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------
async def test_every_outcome_is_audited(write_manifest, ready, api, store) -> None:
    write_manifest()
    await ready()

    await api.call(module="fake", tool="env_names", principal="agent")  # denied
    await api.call(module="fake", tool="add", args={"a": 1, "b": 1}, principal="agent")  # allowed
    await api.call(module="fake", tool="boom", principal="agent")  # tool error
    await api.call(module="fake", tool="nondict", principal="agent")  # malformed
    await api.call(module="fake", tool="sleep", args={"seconds": 0.4}, timeout_s=0.1)  # timeout
    await api.call(module="fake", tool="secretive", args={"secret": "a"}, principal="agent")  # confirm

    rows = {r["tool"]: r for r in await audit(store)}
    assert rows["env_names"]["decision"] == "deny"
    assert rows["add"]["result"] == "ok"
    assert rows["boom"]["result"] == "tool_error"
    assert rows["nondict"]["result"] == "bad_result"
    assert rows["sleep"]["result"] == "timeout"
    assert rows["secretive"]["decision"] == "confirm"
    assert all(r["principal"] for r in rows.values())


async def test_the_audit_log_records_arguments_and_duration(write_manifest, ready, api, store) -> None:
    write_manifest()
    await ready()
    await api.call(module="fake", tool="add", args={"a": 3, "b": 4}, principal="agent")

    row = next(r for r in await audit(store) if r["tool"] == "add")
    assert json.loads(row["args"]) == {"a": 3, "b": 4}
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0


async def test_redacted_arguments_do_not_reach_the_audit_log(write_manifest, ready, api, store) -> None:
    # The manifest asks for `secret` to be redacted. The module still receives
    # the real value; the log that survives the call does not.
    write_manifest()
    await ready()
    pending_id = (
        await api.call(module="fake", tool="secretive", args={"secret": "hunter2"}, principal="agent")
    )["pending_id"]
    result = await api.confirm(pending_id)

    assert result["result"]["received"] == {"secret": "hunter2"}
    for row in await audit(store):
        assert "hunter2" not in (row["args"] or "")
    assert json.loads(next(r for r in await audit(store) if r["tool"] == "secretive")["args"]) == {
        "secret": "***"
    }
