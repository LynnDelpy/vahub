"""The built-in `core` tools: reached through the gate, like any module."""

from __future__ import annotations

from pathlib import Path

import pytest

from vahub.config.models import Config
from vahub.core.builtins import CORE_MODULE, CORE_RULES, TOOL_DEFS, build_core_module

pytestmark = pytest.mark.integration


def test_core_rules_cover_every_tool_and_argument() -> None:
    # A tool the gate has no rule for, or an argument with no constraint, is
    # denied. So the shipped CORE_RULES must name every tool and every argument
    # the tool declares, or the assistant silently cannot use it.
    for name, _cls, _desc, schema in TOOL_DEFS:
        rule = CORE_RULES.get(f"core.{name}")
        assert rule is not None, f"no rule for core.{name}"
        args = set((schema.get("properties") or {}).keys())
        constrained = set((rule.get("constraints") or {}).keys())
        assert args == constrained, f"core.{name}: {args ^ constrained} lack constraints"


@pytest.fixture
async def api_and_store(construct, state_dir: Path, modules_dir: Path):
    from vahub.agent.policy import Gate
    from vahub.core.bus import EventBus
    from vahub.core.moduleapi import ModuleAPI
    from vahub.core.supervisor import Supervisor
    from vahub.scheduler import Scheduler
    from vahub.storage.store import Store

    config = Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "llm": {"provider": "mock"},
            "policy": {
                "default": "deny",
                "principals": {"agent": {"confirm": [], "deny": []}},
                "rules": CORE_RULES,
            },
        }
    )
    store = Store(state_dir / "vahub.db")
    await store.open()
    bus = EventBus()
    sup = Supervisor(bus, modules_dir=modules_dir, state_dir=state_dir, config_dir=modules_dir.parent)
    api = ModuleAPI(sup, gate=Gate(config.policy), store=store, bus=bus)
    scheduler = Scheduler(api, bus, config, store=store)
    sup.modules[CORE_MODULE] = build_core_module(store, scheduler, sup)
    try:
        yield api, store
    finally:
        await store.close()


async def _call(api, tool, args):
    return await api.call(module=CORE_MODULE, tool=tool, args=args, principal="agent")


async def test_location_and_memory_via_the_gate(api_and_store) -> None:
    api, store = api_and_store
    ok = await _call(api, "set_location", {"name": "home", "latitude": 47.4, "longitude": 9.4})
    assert ok["ok"] is True
    assert (await store.get_location("home"))["latitude"] == 47.4

    await _call(api, "remember", {"key": "units", "value": "metric"})
    recalled = await _call(api, "recall", {"key": "units"})
    assert recalled["result"]["value"] == "metric"


async def test_the_gate_still_bounds_a_builtin(api_and_store) -> None:
    api, _store = api_and_store
    # Latitude out of range is refused by the same constraint machinery as a
    # module tool: the built-in is not a way around the gate.
    denied = await _call(api, "set_location", {"name": "x", "latitude": 999})
    assert denied["ok"] is False and denied["error"] == "policy_denied"


async def test_create_schedule_persists_and_runs_as_scheduler(api_and_store) -> None:
    api, store = api_and_store
    result = await _call(
        api,
        "create_schedule",
        {"cron": "0 7 * * *", "module": "time", "tool": "speak_current_time", "args": {}},
    )
    assert result["ok"] is True
    rows = await store.list_dyn_schedules()
    assert len(rows) == 1 and rows[0]["created_by"] == "assistant"


async def test_add_list_and_remove_dashboard_cards(api_and_store) -> None:
    api, store = api_and_store
    # A card must be backed by a declared read tool; core.list_locations is one.
    added = await _call(api, "add_card", {"module": "core", "tool": "list_locations", "title": "Places"})
    assert added["ok"] is True
    card = added["result"]["card"]
    assert card["module"] == "core" and card["tool"] == "list_locations" and card["title"] == "Places"

    # It is stored in the single global dashboard setting the browser reads.
    stored = await store.get_setting("ui:dashboard")
    assert isinstance(stored, list) and [c["id"] for c in stored] == [card["id"]]

    listed = await _call(api, "list_cards", {})
    assert [c["id"] for c in listed["result"]["cards"]] == [card["id"]]

    removed = await _call(api, "remove_card", {"id": card["id"]})
    assert removed["ok"] is True and removed["result"]["removed"] == 1
    assert await store.get_setting("ui:dashboard") == []


async def test_add_card_refuses_write_tools_and_unknown_tools(api_and_store) -> None:
    api, store = api_and_store
    # A write tool cannot back a card (the render path only runs read tools).
    write = await _call(api, "add_card", {"module": "core", "tool": "set_location"})
    assert write["ok"] is False
    # Nor can a tool that does not exist.
    missing = await _call(api, "add_card", {"module": "nope", "tool": "nope"})
    assert missing["ok"] is False
    # Neither attempt left anything behind.
    assert (await store.get_setting("ui:dashboard")) in (None, [])
