"""The catalog: what the model is allowed to know exists.

This is a boundary, not a convenience. A tool the policy would never let the
agent call is not merely denied on use, it is never shown, so the model cannot
plan around it, name it in an answer, or be talked into asking for it. The tests
below are mostly about what is *absent*.
"""

from __future__ import annotations

from typing import Any

from vahub.agent.policy import Gate
from vahub.config.models import PolicyConfig
from vahub.contracts.manifest import Manifest
from vahub.core.catalog import Catalog
from vahub.core.supervisor import Module, State


def _module(
    name: str,
    tools: dict[str, str],
    live: list[str] | None = None,
    state: State = State.READY,
) -> Module:
    """A module as the supervisor holds one: a manifest (what it claims) and a
    live tool list (what the running process actually offered)."""
    manifest = Manifest.model_validate(
        {
            "name": name,
            "version": "1.0.0",
            "runtime": {"command": ["true"]},
            "tools": {tool: {"class": cls} for tool, cls in tools.items()},
        }
    )
    offered = live if live is not None else list(tools)
    return Module(
        manifest=manifest,
        state=state,
        client=object(),
        tools=[{"name": t, "description": f"does {t}", "inputSchema": {"type": "object"}} for t in offered],
    )


class _Sup:
    def __init__(self, modules: dict[str, Module]) -> None:
        self.modules = modules


def _read_rules(*names: str) -> dict[str, Any]:
    return {name: {"class": "read", "constraints": {}} for name in names}


def _catalog(modules: dict[str, Module], rules: dict[str, Any] | None = None, **extra: Any) -> Catalog:
    """The config refuses several footguns outright (default=allow with no rules,
    a destructive rule with no agent principal to confirm it), which is why every
    policy here is spelled out rather than waved at."""
    policy = PolicyConfig.model_validate({"default": "deny", "rules": rules or {}, **extra})
    return Catalog(_Sup(modules), gate=Gate(policy))


def test_a_tool_no_rule_names_is_not_shown_to_the_agent() -> None:
    modules = {"light": _module("light", {"turn_on": "write", "status": "read"})}
    cat = _catalog(modules, rules={"light.status": {"class": "read", "constraints": {}}})
    # Everything the process offers is in the full listing...
    assert [t["name"] for t in cat.list_tools()] == ["light.status", "light.turn_on"]
    # ...but the model is only told about what it could actually call.
    assert [t["name"] for t in cat.agent_catalog()] == ["light.status"]


def test_the_health_probe_is_never_in_the_catalog() -> None:
    modules = {"light": _module("light", {"status": "read"}, live=["status", "__health"])}
    cat = _catalog(modules, rules={"light.status": {"class": "read", "constraints": {}}})
    names = [t["tool"] for t in cat.list_tools()]
    assert "__health" not in names and names == ["status"]


def test_a_module_that_is_not_ready_offers_nothing() -> None:
    modules = {"light": _module("light", {"status": "read"}, state=State.DEGRADED)}
    cat = _catalog(modules, rules={"light.status": {"class": "read", "constraints": {}}})
    assert cat.list_tools() == []
    # and the agent is told the module exists but cannot be reached
    assert cat.unavailable_modules() == ["light"]


def test_the_live_list_wins_over_the_manifest() -> None:
    """The manifest is the author's description; the process is the truth. A
    tool a manifest declares but the process never offered must not be callable,
    and one the process offers but the manifest forgot is still real."""
    modules = {"light": _module("light", {"declared_only": "read"}, live=["actually_offered"])}
    cat = _catalog(modules, _read_rules("light.declared_only", "light.actually_offered"))
    names = [t["tool"] for t in cat.list_tools()]
    assert names == ["actually_offered"]
    assert cat.resolve("light.declared_only") is None
    assert cat.resolve("light.actually_offered") == ("light", "actually_offered")


def test_the_declared_class_is_carried_but_is_only_advisory() -> None:
    modules = {"door": _module("door", {"unlock": "read"})}  # the module under-claims
    policy = {
        "default": "deny",
        "rules": {"door.unlock": {"class": "destructive", "constraints": {}}},
        "principals": {"agent": {"confirm": ["destructive"], "deny": []}},
    }
    cat = Catalog(_Sup(modules), gate=Gate(PolicyConfig.model_validate(policy)))
    tool = cat.list_tools()[0]
    assert tool["declared_class"] == "read"  # what the module said
    # What governs is the policy, and the catalog does not pretend otherwise:
    # the gate is asked separately at call time, and it says destructive.
    assert Gate(PolicyConfig.model_validate(policy)).cls_for("door", "unlock") == "destructive"


def test_resolve_refuses_a_name_that_is_not_ours() -> None:
    modules = {"light": _module("light", {"status": "read"})}
    cat = _catalog(modules, _read_rules("light.status"))
    assert cat.resolve("nomodule.status") is None
    assert cat.resolve("light.nosuchtool") is None
    assert cat.resolve("nodot") is None
    assert cat.resolve("") is None


def test_two_modules_may_both_offer_status() -> None:
    """The namespace is the reason the catalog exists: one unambiguous string."""
    modules = {
        "light": _module("light", {"status": "read"}),
        "door": _module("door", {"status": "read"}),
    }
    cat = _catalog(modules, _read_rules("door.status", "light.status"))
    assert [t["name"] for t in cat.list_tools()] == ["door.status", "light.status"]
    assert cat.resolve("door.status") == ("door", "status")
