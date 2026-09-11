"""Booting the hub and stopping it again, over a real socket.

`Runtime.run()` is the one path the rest of the suite never takes: every other
test builds the pieces and drives them directly. What is being checked is the
order, which the source says in comments is load-bearing and which a comment
cannot enforce:

* module configuration from the database is loaded before discovery, so a module
  configured through the web UI starts on this boot rather than the next one;
* the built-in `core` module is registered after the supervisor has started, so
  it is never handed to the spawn loop that expects a process.

And that stopping is clean: the server task ends, the store is closed, and the
background tasks are cancelled rather than left running into the next test.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest

from vahub.config.models import Config
from vahub.core.builtins import CORE_MODULE

pytestmark = pytest.mark.integration


def free_port() -> int:
    """Bind port 0, note what the kernel handed out, release it. A fixed port
    makes the suite fail for whoever already has something on it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def booted(construct, state_dir: Path, modules_dir: Path):
    """A Runtime taken through `run()` and stopped again, yielded while up.

    The store is deliberately not opened here: opening it is part of what run()
    is being tested for.
    """
    from vahub.core.runtime import Runtime

    port = free_port()
    config = Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "web": {"host": "127.0.0.1", "port": port, "auth": {"enabled": False}},
            "llm": {"provider": "mock"},
            "policy": {"default": "deny", "rules": {}},
        }
    )
    rt = construct(Runtime, config=config, config_path=modules_dir.parent / "vahub.yaml")
    task = asyncio.create_task(rt.run(), name="hub-under-test")

    deadline = asyncio.get_running_loop().time() + 10.0
    while asyncio.get_running_loop().time() < deadline:
        if getattr(rt._server, "started", False):
            break
        if task.done():  # it fell over on the way up; surface that, not a timeout
            await task
        await asyncio.sleep(0.02)
    else:
        task.cancel()
        raise AssertionError("the hub did not come up within 10s")

    try:
        yield rt, port, task
    finally:
        rt.request_stop()
        await asyncio.wait_for(task, timeout=10.0)


async def test_the_hub_serves_over_a_real_socket_once_it_is_up(booted) -> None:
    _, port, _ = booted

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        response = await client.get("/health")

    assert response.status_code == 200


async def test_run_opens_the_store_itself(booted) -> None:
    rt, _, _ = booted

    # run() is what opens it; nothing in the fixture did.
    assert rt.store._db is not None


async def test_the_built_in_module_is_registered_and_has_no_process(booted) -> None:
    rt, _, _ = booted

    module = rt.supervisor.modules[CORE_MODULE]

    assert module is not None
    assert getattr(module, "process", None) is None


async def test_run_loads_stored_config_before_discovery_and_builtins_after_start(
    construct, state_dir: Path, modules_dir: Path
) -> None:
    """The two orderings run() carries comments about, pinned.

    Asserting that the built-in module merely exists would pass however run() is
    ordered, so what is recorded here is what the supervisor could see at the
    moment its spawn loop ran.
    """
    from vahub.core.runtime import Runtime

    port = free_port()
    config = Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "web": {"host": "127.0.0.1", "port": port, "auth": {"enabled": False}},
            "llm": {"provider": "mock"},
            "policy": {"default": "deny", "rules": {}},
        }
    )
    rt = construct(Runtime, config=config, config_path=modules_dir.parent / "vahub.yaml")

    order: list[str] = []
    visible_to_the_spawn_loop: set[str] = set()

    def recorded(name, original):
        def wrapper(*args, **kwargs):
            order.append(name)
            return original(*args, **kwargs)

        return wrapper

    real_start = rt.supervisor.start

    async def start(*args, **kwargs):
        order.append("start")
        visible_to_the_spawn_loop.update(rt.supervisor.modules)
        return await real_start(*args, **kwargs)

    rt.supervisor.set_db_config = recorded("set_db_config", rt.supervisor.set_db_config)
    rt.supervisor.discover = recorded("discover", rt.supervisor.discover)
    rt.supervisor.start = start
    rt._register_builtins = recorded("register_builtins", rt._register_builtins)

    task = asyncio.create_task(rt.run(), name="hub-under-test")
    while not getattr(rt._server, "started", False):
        if task.done():
            await task
        await asyncio.sleep(0.02)
    rt.request_stop()
    await asyncio.wait_for(task, timeout=10.0)

    assert order == ["set_db_config", "discover", "start", "register_builtins"]
    # The synthetic module has no process, so the spawn loop must never see it.
    assert CORE_MODULE not in visible_to_the_spawn_loop


async def test_module_state_changes_reach_the_database(booted) -> None:
    rt, _, _ = booted

    rt.bus.publish("module.state_changed", {"module": "fake", "state": "ready", "last_error": None})

    deadline = asyncio.get_running_loop().time() + 5.0
    saved = None
    while asyncio.get_running_loop().time() < deadline:
        rows = {row["module"]: row for row in await rt.store.module_states()}
        if "fake" in rows:
            saved = rows["fake"]
            break
        await asyncio.sleep(0.02)

    assert saved is not None, "the state_changed subscriber never persisted the event"
    assert saved["state"] == "ready"


async def test_stopping_closes_the_store_and_ends_every_background_task(
    construct, state_dir: Path, modules_dir: Path
) -> None:
    from vahub.core.runtime import Runtime

    port = free_port()
    config = Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "web": {"host": "127.0.0.1", "port": port, "auth": {"enabled": False}},
            "llm": {"provider": "mock"},
            "policy": {"default": "deny", "rules": {}},
        }
    )
    rt = construct(Runtime, config=config, config_path=modules_dir.parent / "vahub.yaml")
    task = asyncio.create_task(rt.run(), name="hub-under-test")
    while not getattr(rt._server, "started", False):
        if task.done():
            await task
        await asyncio.sleep(0.02)

    rt.request_stop()
    await asyncio.wait_for(task, timeout=10.0)

    assert rt.store._db is None
    assert rt._background == []
