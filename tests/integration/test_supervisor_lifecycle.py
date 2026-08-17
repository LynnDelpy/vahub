"""Runtime module lifecycle: install, configure and remove without a restart.

The web UI installs a module, sets its token, and removes it while the hub is
running. These exercise the supervisor methods that back those actions and the
database-backed configuration that lets a token entered in a browser reach a
module's environment, which the host environment never carried.
"""

from __future__ import annotations

import asyncio

import pytest

from vahub.core.supervisor import State

pytestmark = pytest.mark.integration


async def call(mod, tool: str, args: dict | None = None, timeout: float = 10.0):
    async with mod.lock:
        return await mod.client.call_tool(tool, args or {}, timeout)


# --------------------------------------------------------------------------
# database-backed configuration reaches the child
# --------------------------------------------------------------------------
async def test_stored_config_is_passed_to_the_child_environment(write_manifest, supervisor) -> None:
    # A value set in the UI (stored in the database) must reach the module even
    # though nothing in the host environment carries it.
    supervisor.set_db_config({"fake": {"FAKE_NAME": "from-database"}})
    write_manifest("fake")
    supervisor.discover()
    env = supervisor._child_env(supervisor.modules["fake"].manifest)
    assert env["FAKE_NAME"] == "from-database"


async def test_host_environment_wins_over_stored_config(
    write_manifest, supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An operator exporting a value for an incident must not be silently
    # overridden by an older value a web form left in the database.
    monkeypatch.setenv("FAKE_NAME", "from-host")
    supervisor.set_db_config({"fake": {"FAKE_NAME": "from-database"}})
    write_manifest("fake")
    supervisor.discover()
    assert supervisor._child_env(supervisor.modules["fake"].manifest)["FAKE_NAME"] == "from-host"


# --------------------------------------------------------------------------
# configure a module that was missing a required key
# --------------------------------------------------------------------------
async def test_providing_a_required_key_via_config_starts_the_module(
    write_manifest, supervisor, spawn, wait_for
) -> None:
    write_manifest("needsdb", config={"required": ["FAKE_NAME"], "optional": []})
    await spawn()
    mod = supervisor.modules["needsdb"]
    assert mod.state == State.UNCONFIGURED
    assert mod.proc is None

    supervisor.update_module_config("needsdb", {"FAKE_NAME": "provided"})
    assert await supervisor.apply_config("needsdb") is True
    await wait_for(lambda: mod.state == State.READY, timeout=20.0)
    assert "FAKE_NAME" in set((await call(mod, "env_names"))["structuredContent"]["names"])


async def test_removing_a_required_key_stops_the_module(write_manifest, supervisor, spawn, wait_for) -> None:
    supervisor.set_db_config({"needsdb": {"FAKE_NAME": "provided"}})
    write_manifest("needsdb", config={"required": ["FAKE_NAME"], "optional": []})
    await spawn()
    mod = supervisor.modules["needsdb"]
    await wait_for(lambda: mod.state == State.READY, timeout=20.0)

    supervisor.update_module_config("needsdb", {})  # the UI cleared the token
    assert await supervisor.apply_config("needsdb") is True
    await wait_for(lambda: mod.state == State.UNCONFIGURED, timeout=10.0)
    assert mod.proc is None or mod.proc.returncode is not None


# --------------------------------------------------------------------------
# install and remove at runtime
# --------------------------------------------------------------------------
async def test_load_module_starts_a_module_installed_after_startup(
    write_manifest, supervisor, spawn, wait_for
) -> None:
    await spawn()  # nothing installed yet
    write_manifest("late")  # the installer just wrote this manifest
    assert await supervisor.load_module("late") is True
    await wait_for(lambda: supervisor.modules["late"].state == State.READY, timeout=20.0)


async def test_load_module_is_a_clean_no_op_for_an_unknown_name(supervisor, spawn) -> None:
    await spawn()
    assert await supervisor.load_module("does-not-exist") is False


async def test_reinstalling_a_running_module_picks_up_the_new_manifest(
    write_manifest, supervisor, spawn, wait_for
) -> None:
    # A reinstall from the UI rewrites the manifest on disk; loading it again must
    # replace the live instance rather than keep running the old one.
    write_manifest("relo", version="1.0.0")
    await spawn()
    mod = supervisor.modules["relo"]
    await wait_for(lambda: mod.state == State.READY, timeout=20.0)
    first_pid = mod.proc.pid

    write_manifest("relo", version="2.0.0")  # the installer just rewrote it
    assert await supervisor.load_module("relo") is True
    await wait_for(
        lambda: (
            supervisor.modules["relo"].state == State.READY
            and supervisor.modules["relo"].proc.pid != first_pid
        ),
        timeout=20.0,
    )
    assert supervisor.modules["relo"].manifest.version == "2.0.0"


async def test_remove_module_stops_and_forgets_it(write_manifest, supervisor, ready) -> None:
    write_manifest("fake")
    mod = await ready("fake")
    proc = mod.proc
    assert await supervisor.remove_module("fake") is True
    assert "fake" not in supervisor.modules
    assert "fake" not in supervisor._tasks  # no supervise task left tracking it
    assert proc.returncode is not None  # the child did not outlive the removal


async def test_concurrent_lifecycle_calls_do_not_leak_a_process(
    write_manifest, supervisor, spawn, wait_for
) -> None:
    # A stop racing a reinstall must not leave two supervise tasks or an orphaned
    # child. The lifecycle lock serialises them whichever order they land in.
    write_manifest("ser")
    await spawn()
    await wait_for(lambda: supervisor.modules["ser"].state == State.READY, timeout=20.0)
    live = supervisor.modules["ser"].proc

    write_manifest("ser", version="2.0.0")  # a reinstall
    await asyncio.gather(supervisor.load_module("ser"), supervisor.stop_module("ser"))

    assert sum(1 for n in supervisor._tasks if n == "ser") <= 1
    await supervisor.stop()
    assert live.returncode is not None  # the original process was reaped, not orphaned


async def test_stop_module_leaves_the_row_but_kills_the_process(
    write_manifest, supervisor, ready, wait_for
) -> None:
    write_manifest("fake")
    mod = await ready("fake")
    assert await supervisor.stop_module("fake") is True
    assert supervisor.modules["fake"].state == State.STOPPED
    assert mod.proc.returncode is not None
