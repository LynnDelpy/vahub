"""The supervisor against a real child process.

These tests spawn tests/integration/fake_module.py, so they exercise the parts
that cannot be faked usefully: pipes, a process that exits, a handshake that
never completes, and output designed to break the reader. The recurring
assertion is that the hub survives. A module is someone else's program, and the
only acceptable outcome of it behaving badly is that the module stops working.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from vahub.core.mcpclient import McpError
from vahub.core.supervisor import State

pytestmark = pytest.mark.integration


async def call(mod, tool: str, args: dict | None = None, timeout: float = 10.0):
    """Call a tool the way the hub does: holding the module lock, so a probe and
    a call never overlap."""
    async with mod.lock:
        return await mod.client.call_tool(tool, args or {}, timeout)


def payload(raw: dict):
    return raw["structuredContent"]


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------
async def test_a_module_starts_shakes_hands_and_lists_its_tools(write_manifest, ready) -> None:
    write_manifest()
    mod = await ready()

    assert mod.state == State.READY
    names = {t["name"] for t in mod.tools}
    assert {"echo", "add", "__health"} <= names
    assert mod.proc is not None and mod.proc.returncode is None


async def test_a_tool_call_returns_the_modules_answer(write_manifest, ready) -> None:
    write_manifest()
    mod = await ready()
    assert payload(await call(mod, "add", {"a": 2, "b": 3})) == {"sum": 5}


async def test_the_module_only_sees_the_environment_it_declared(
    write_manifest, ready, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point of one process per integration is that a token belonging
    # to one module is not readable by another.
    monkeypatch.setenv("FAKE_NAME", "declared")
    monkeypatch.setenv("HA_TOKEN", "another-modules-secret")
    write_manifest()
    mod = await ready()

    names = set(payload(await call(mod, "env_names"))["names"])
    assert "FAKE_NAME" in names
    assert "HA_TOKEN" not in names
    assert "PATH" in names  # it still has to be able to find a shared library


async def test_stderr_is_captured_rather_than_lost(
    write_manifest, ready, bus, collect, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_STDERR", "hello from the module")
    lines = collect(bus, "module.log")
    write_manifest()
    mod = await ready()

    await asyncio.sleep(0.2)
    assert any("hello from the module" in line for line in mod.stderr_ring)
    assert any("hello from the module" in event.get("line", "") for event in lines)


async def test_state_changes_are_published(write_manifest, bus, collect, ready) -> None:
    events = collect(bus, "module.state_changed")
    write_manifest()
    await ready()
    await asyncio.sleep(0.05)
    assert [e["state"] for e in events][-1] == State.READY.value
    assert all(e["module"] == "fake" for e in events)


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------
async def test_an_unreachable_backend_degrades_the_module_without_restarting_it(
    write_manifest, ready, supervisor, wait_for
) -> None:
    write_manifest()
    mod = await ready()
    pid = mod.proc.pid

    await call(mod, "set_health", {"ok": False, "detail": "backend unreachable"})
    await wait_for(lambda: mod.state == State.DEGRADED)

    # Degraded is not failed: restarting the process would not make someone
    # else's server come back, it would only lose the module's own state.
    assert mod.proc.pid == pid
    assert mod.restarts == 0
    assert mod.health.get("ok") is False

    await call(mod, "set_health", {"ok": True})
    await wait_for(lambda: mod.state == State.READY)
    assert mod.proc.pid == pid


async def test_health_probes_do_not_overlap_a_tool_call(write_manifest, ready) -> None:
    # One in-flight call per module: the fake module answers strictly in order,
    # so a probe landing mid-call would deadlock or cross the answers over.
    write_manifest()
    mod = await ready()
    for i in range(6):
        assert payload(await call(mod, "echo", {"text": str(i)}))["args"] == {"text": str(i)}
        await asyncio.sleep(0.05)
    assert mod.state == State.READY


# --------------------------------------------------------------------------
# a module that misbehaves
# --------------------------------------------------------------------------
async def test_a_result_that_is_not_an_object_does_not_break_the_connection(write_manifest, ready) -> None:
    write_manifest()
    mod = await ready()

    assert await call(mod, "nondict") == "not-an-object"
    # The connection has to survive it, or one sloppy module is a denial of service.
    assert payload(await call(mod, "echo", {"text": "still here"}))["args"] == {"text": "still here"}
    assert mod.state == State.READY


async def test_a_message_with_an_unhashable_id_is_discarded(write_manifest, ready) -> None:
    # A list is not a legal JSON-RPC id. Using it as a dict key raises inside
    # the read loop, which would take every pending call down with it.
    write_manifest()
    mod = await ready()

    assert payload(await call(mod, "badid")) == {"survived": True}
    assert payload(await call(mod, "add", {"a": 1, "b": 1})) == {"sum": 2}


async def test_a_server_to_client_request_is_refused_but_not_fatal(write_manifest, ready) -> None:
    # We announce no client capabilities, so a module asking us to sample from
    # the model gets an error back and its own call still completes.
    write_manifest()
    mod = await ready()

    assert payload(await call(mod, "srvreq")) == {"asked": True}
    assert payload(await call(mod, "echo", {"text": "after"}))["args"] == {"text": "after"}


async def test_a_tool_error_is_returned_as_an_error_result(write_manifest, ready) -> None:
    write_manifest()
    mod = await ready()
    raw = await call(mod, "boom")
    assert raw["isError"] is True


async def test_an_oversized_line_breaks_only_that_module(write_manifest, ready, supervisor, wait_for) -> None:
    write_manifest("fake")
    write_manifest("other")
    await ready("fake")
    await ready("other")
    broken, healthy = supervisor.modules["fake"], supervisor.modules["other"]

    # A line past the reader's limit leaves the stream unrecoverable. It must
    # surface as a failed call, not as an exception escaping the read task.
    with pytest.raises(McpError):
        await call(broken, "oversized", {"n": 2_000_000}, timeout=10.0)

    await wait_for(lambda: broken.state == State.DEGRADED)
    assert healthy.state == State.READY
    assert payload(await call(healthy, "echo", {"text": "unaffected"}))["args"] == {"text": "unaffected"}


async def test_a_slow_module_times_out_without_cancelling_the_hub(write_manifest, ready) -> None:
    write_manifest()
    mod = await ready()

    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await call(mod, "sleep", {"seconds": 0.6}, timeout=0.15)

    # The late answer belongs to nobody now and must not be handed to the next
    # caller. Asking a different question must give that question's answer.
    assert payload(await call(mod, "echo", {"text": "hallway"}))["args"] == {"text": "hallway"}


# --------------------------------------------------------------------------
# restart and failure
# --------------------------------------------------------------------------
async def test_a_module_that_exits_is_restarted(write_manifest, ready, supervisor, wait_for) -> None:
    write_manifest()
    mod = await ready()
    first_pid = mod.proc.pid

    with pytest.raises((McpError, TimeoutError, asyncio.TimeoutError)):
        await call(mod, "crash", timeout=2.0)

    await wait_for(lambda: mod.state == State.READY and mod.proc.pid != first_pid, timeout=20.0)
    assert mod.restarts >= 1
    assert payload(await call(mod, "echo", {"text": "back"}))["args"] == {"text": "back"}


async def test_restarts_are_spaced_out(write_manifest, supervisor, spawn, wait_for) -> None:
    # A module that cannot start must not be retried in a tight loop.
    manifest = write_manifest(
        "brokenexit",
        runtime={"command": [sys.executable, "-c", "raise SystemExit(1)"]},
        restart={"max_retries": 2, "backoff_base_s": 1.2, "reset_after_s": 600, "startup_timeout_s": 5},
    )
    assert manifest.restart.max_retries == 2

    started = time.monotonic()
    await spawn()
    mod = supervisor.modules["brokenexit"]
    await wait_for(lambda: mod.state == State.FAILED, timeout=30.0)
    elapsed = time.monotonic() - started

    assert mod.restarts > manifest.restart.max_retries
    assert elapsed >= manifest.restart.backoff_base_s, "retries happened with no delay at all"


async def test_a_module_whose_handshake_never_completes_gives_up(
    write_manifest, supervisor, spawn, wait_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HANDSHAKE_HANG", "1")
    write_manifest(
        "hanger",
        restart={"max_retries": 0, "backoff_base_s": 1.05, "reset_after_s": 600, "startup_timeout_s": 1},
    )
    await spawn()
    mod = supervisor.modules["hanger"]

    await wait_for(lambda: mod.state == State.FAILED, timeout=20.0)
    # The process must be reaped, not left holding the pipe open forever.
    await wait_for(lambda: mod.proc is None or mod.proc.returncode is not None, timeout=10.0)
    assert "handshake" in (mod.last_error or "").lower() or "timeout" in (mod.last_error or "").lower()


async def test_a_module_announcing_no_tools_is_not_usable(
    write_manifest, supervisor, spawn, wait_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_NO_TOOLS_CAP", "1")
    write_manifest(
        "notools",
        restart={"max_retries": 0, "backoff_base_s": 1.05, "reset_after_s": 600, "startup_timeout_s": 5},
    )
    await spawn()
    mod = supervisor.modules["notools"]
    await wait_for(lambda: mod.state == State.FAILED, timeout=20.0)
    assert mod.state != State.READY


async def test_a_module_missing_its_configuration_is_never_spawned(write_manifest, supervisor, spawn) -> None:
    write_manifest("needsconf", config={"required": ["NOT_IN_THE_ENVIRONMENT"], "optional": []})
    await spawn()
    mod = supervisor.modules["needsconf"]

    assert mod.state == State.UNCONFIGURED
    assert mod.proc is None
    assert "NOT_IN_THE_ENVIRONMENT" in (mod.last_error or "")


async def test_a_module_that_cannot_be_executed_fails_without_taking_others_down(
    write_manifest, supervisor, spawn, wait_for, ready
) -> None:
    write_manifest("fake")
    write_manifest(
        "nosuchbinary",
        runtime={"command": ["/nonexistent/vahub-module-binary"]},
        restart={"max_retries": 0, "backoff_base_s": 1.05, "reset_after_s": 600, "startup_timeout_s": 5},
    )
    await ready("fake")

    broken = supervisor.modules["nosuchbinary"]
    await wait_for(lambda: broken.state == State.FAILED, timeout=20.0)
    assert supervisor.modules["fake"].state == State.READY


async def test_an_invalid_manifest_does_not_stop_the_other_modules(
    write_manifest, modules_dir: Path, ready, supervisor
) -> None:
    write_manifest("fake")
    (modules_dir / "broken.yaml").write_text("name: Not A Valid Name\nruntime: {command: []}\n")

    await ready("fake")
    assert "fake" in supervisor.modules


# --------------------------------------------------------------------------
# shutdown
# --------------------------------------------------------------------------
async def test_stop_terminates_every_child(write_manifest, ready, supervisor) -> None:
    write_manifest("fake")
    write_manifest("other")
    await ready("fake")
    await ready("other")
    procs = [m.proc for m in supervisor.modules.values() if m.proc is not None]
    assert len(procs) == 2

    await supervisor.stop()

    for proc in procs:
        assert proc.returncode is not None, "a child outliving the hub is an orphan"
    assert all(m.state == State.STOPPED for m in supervisor.modules.values())


async def test_stop_is_safe_to_call_twice(write_manifest, ready, supervisor) -> None:
    write_manifest()
    await ready()
    await supervisor.stop()
    await supervisor.stop()


async def test_stop_does_not_restart_what_it_just_stopped(write_manifest, ready, supervisor) -> None:
    write_manifest()
    mod = await ready()
    await supervisor.stop()
    await asyncio.sleep(0.3)
    assert mod.state == State.STOPPED
    assert mod.proc.returncode is not None


def test_the_fake_module_is_where_the_tests_think_it_is() -> None:
    # A missing test module would otherwise show up as every integration test
    # timing out on a handshake.
    assert (Path(__file__).parent / "fake_module.py").is_file()
