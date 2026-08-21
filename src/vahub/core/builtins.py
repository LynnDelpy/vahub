"""Built-in tools: the hub's own data, offered to the agent like any module.

The assistant can save a location, remember a fact, and create a schedule
because a synthetic module named ``core`` offers tools for exactly those things.
It is not a subprocess: its "client" dispatches in process to functions that read
and write the hub's database. Everything else is unchanged, which is the point.
The call still goes through the policy gate (there are ``core.*`` rules in
vahub.yaml), it is still audited, and the catalog still hides a tool the agent
may not use. So the agent editing saved data is bounded exactly like the agent
turning on a light.

The boundary the design keeps: these tools reach saved data and schedules, never
the policy or the accounts. A schedule they create still runs as principal
``scheduler``, so it cannot do anything the scheduler is not already allowed to.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..__about__ import __version__
from ..contracts.manifest import Manifest
from .supervisor import Module, State

if TYPE_CHECKING:
    from ..scheduler import Scheduler
    from ..storage.store import Store
    from .supervisor import Supervisor

CORE_MODULE = "core"

# The home dashboard is one global list of cards (built-in widgets and cards that
# show a module read-tool's result). It lives in a single setting so the agent,
# which has no per-request user context, can pin a card the browser will show.
DASHBOARD_KEY = "ui:dashboard"

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


class BuiltinError(Exception):
    """A tool-level failure. Turned into an MCP isError result, not a crash."""


class BuiltinClient:
    """Stands in for an McpClient. `call_tool` dispatches to an in-process handler
    and returns the same result shape a real module would send over MCP."""

    def __init__(self, handlers: dict[str, Handler]) -> None:
        self._handlers = handlers

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: float) -> Any:
        handler = self._handlers.get(name)
        if handler is None:
            return _err(f"unknown builtin tool {name!r}")
        try:
            payload = await handler(arguments or {})
        except BuiltinError as e:
            return _err(str(e))
        return {
            "structuredContent": payload,
            "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
            "isError": False,
        }

    async def close(self) -> None:  # parity with McpClient; nothing to tear down
        return None


def _err(message: str) -> dict[str, Any]:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


# --------------------------------------------------------------------------
# tool definitions: (name, class, description, JSON input schema)
# --------------------------------------------------------------------------
def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_STR = {"type": "string"}
_NUM = {"type": "number"}

TOOL_DEFS: list[tuple[str, str, str, dict[str, Any]]] = [
    (
        "list_locations",
        "read",
        "List the saved places (home, work, ...) with their coordinates.",
        _obj({}),
    ),
    (
        "set_location",
        "write",
        "Save or update a named place. Provide coordinates or an address.",
        _obj(
            {
                "name": {**_STR, "maxLength": 40},
                "label": {**_STR, "maxLength": 80},
                "latitude": {**_NUM, "minimum": -90, "maximum": 90},
                "longitude": {**_NUM, "minimum": -180, "maximum": 180},
                "address": {**_STR, "maxLength": 200},
            },
            required=["name"],
        ),
    ),
    (
        "delete_location",
        "write",
        "Delete a saved place by name.",
        _obj({"name": {**_STR, "maxLength": 40}}, required=["name"]),
    ),
    (
        "remember",
        "write",
        "Remember a fact or preference under a key, e.g. units=metric or anniversary=2018-06-01.",
        _obj(
            {"key": {**_STR, "maxLength": 60}, "value": {**_STR, "maxLength": 500}},
            required=["key", "value"],
        ),
    ),
    (
        "recall",
        "read",
        "Recall a single remembered value by key.",
        _obj({"key": {**_STR, "maxLength": 60}}, required=["key"]),
    ),
    (
        "list_memory",
        "read",
        "List everything the assistant has been asked to remember.",
        _obj({}),
    ),
    (
        "forget",
        "write",
        "Forget a remembered key.",
        _obj({"key": {**_STR, "maxLength": 60}}, required=["key"]),
    ),
    (
        "list_schedules",
        "read",
        "List the recurring routines, both file-defined and runtime-created.",
        _obj({}),
    ),
    (
        "create_schedule",
        "write",
        "Create a recurring routine that calls one tool on a cron schedule. It "
        "runs unattended as the scheduler, so it can only do what the scheduler "
        "is allowed to do.",
        _obj(
            {
                "cron": {**_STR, "maxLength": 100},
                "module": {**_STR, "maxLength": 40},
                "tool": {**_STR, "maxLength": 60},
                "args": {"type": "object"},
                "description": {**_STR, "maxLength": 120},
            },
            required=["cron", "module", "tool"],
        ),
    ),
    (
        "delete_schedule",
        "write",
        "Delete a runtime-created schedule by id.",
        _obj({"id": {**_STR, "maxLength": 40}}, required=["id"]),
    ),
    (
        "set_schedule_enabled",
        "write",
        "Enable or disable a runtime-created schedule.",
        _obj(
            {"id": {**_STR, "maxLength": 40}, "enabled": {"type": "boolean"}},
            required=["id", "enabled"],
        ),
    ),
    (
        "list_cards",
        "read",
        "List the cards pinned to the home dashboard.",
        _obj({}),
    ),
    (
        "add_card",
        "write",
        "Pin a card to the home dashboard that shows the live result of a module's read-only tool. "
        'For example module "transit" tool "next_departures" with args {"station": "Zurich HB"} pins a '
        'departures board; module "weather" tool "forecast" pins the weather. Pick a read tool from '
        "the module catalog and give the arguments it needs. Give a short human title when you can.",
        _obj(
            {
                "module": {**_STR, "maxLength": 40},
                "tool": {**_STR, "maxLength": 60},
                "args": {"type": "object"},
                "title": {**_STR, "maxLength": 80},
            },
            required=["module", "tool"],
        ),
    ),
    (
        "remove_card",
        "write",
        "Remove a pinned dashboard card, by its id or by the module and tool it shows.",
        _obj(
            {
                "id": {**_STR, "maxLength": 120},
                "module": {**_STR, "maxLength": 40},
                "tool": {**_STR, "maxLength": 60},
            },
        ),
    ),
]


# The policy rules that let the AGENT use these tools. The web UI edits the same
# data through authenticated REST routes that do not go through the gate (the
# person is acting directly, like editing the config), but the agent is gated
# like it is for any other tool. Kept here so the scaffold and the example config
# stay in step with the tool definitions above. Every argument a tool accepts
# needs a constraint, or the gate refuses the call.
CORE_RULES: dict[str, dict[str, Any]] = {
    "core.list_locations": {"class": "read", "constraints": {}},
    "core.set_location": {
        "class": "write",
        "constraints": {
            "name": {"max_len": 40},
            "label": {"max_len": 80},
            "latitude": {"range": [-90, 90]},
            "longitude": {"range": [-180, 180]},
            "address": {"max_len": 200},
        },
    },
    "core.delete_location": {"class": "write", "constraints": {"name": {"max_len": 40}}},
    "core.remember": {
        "class": "write",
        "constraints": {"key": {"max_len": 60}, "value": {"max_len": 500}},
    },
    "core.recall": {"class": "read", "constraints": {"key": {"max_len": 60}}},
    "core.list_memory": {"class": "read", "constraints": {}},
    "core.forget": {"class": "write", "constraints": {"key": {"max_len": 60}}},
    "core.list_schedules": {"class": "read", "constraints": {}},
    "core.create_schedule": {
        "class": "write",
        "constraints": {
            "cron": {"max_len": 100},
            "module": {"max_len": 40},
            "tool": {"max_len": 60},
            "args": {"max_len": 20},
            "description": {"max_len": 120},
        },
    },
    "core.delete_schedule": {"class": "write", "constraints": {"id": {"max_len": 40}}},
    "core.set_schedule_enabled": {
        "class": "write",
        "constraints": {"id": {"max_len": 40}, "enabled": {"in": [True, False]}},
    },
    "core.list_cards": {"class": "read", "constraints": {}},
    "core.add_card": {
        "class": "write",
        "constraints": {
            "module": {"max_len": 40},
            "tool": {"max_len": 60},
            "args": {"max_len": 20},
            "title": {"max_len": 80},
        },
    },
    "core.remove_card": {
        "class": "write",
        "constraints": {"id": {"max_len": 120}, "module": {"max_len": 40}, "tool": {"max_len": 60}},
    },
}


def _card_id(module: str, tool: str, args: dict[str, Any]) -> str:
    """A stable id for a card, so pinning the same tool with the same arguments
    twice replaces rather than duplicates."""
    base = f"{module}.{tool}"
    if not args:
        return base
    digest = hashlib.sha1(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:8]
    return f"{base}:{digest}"


def _default_title(module: str, tool: str, args: dict[str, Any]) -> str:
    words = tool.replace("_", " ").strip()
    label = (words[:1].upper() + words[1:]) if words else module
    for key in ("station", "from", "query", "name", "location", "city", "q"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return f"{label} · {value.strip()}"
    return label


def _handlers(store: Store, scheduler: Scheduler, supervisor: Supervisor) -> dict[str, Handler]:
    async def list_locations(_: dict[str, Any]) -> Any:
        return {"locations": await store.list_locations()}

    async def set_location(a: dict[str, Any]) -> Any:
        name = str(a.get("name") or "").strip()
        if not name:
            raise BuiltinError("name is required")
        await store.upsert_location(
            name,
            label=a.get("label"),
            latitude=a.get("latitude"),
            longitude=a.get("longitude"),
            address=a.get("address"),
        )
        return {"ok": True, "name": name}

    async def delete_location(a: dict[str, Any]) -> Any:
        removed = await store.delete_location(str(a.get("name") or ""))
        return {"ok": removed, "name": a.get("name")}

    async def remember(a: dict[str, Any]) -> Any:
        key = str(a.get("key") or "").strip()
        if not key:
            raise BuiltinError("key is required")
        await store.set_setting(f"memory:{key}", a.get("value"))
        return {"ok": True, "key": key}

    async def recall(a: dict[str, Any]) -> Any:
        key = str(a.get("key") or "")
        return {"key": key, "value": await store.get_setting(f"memory:{key}")}

    async def list_memory(_: dict[str, Any]) -> Any:
        prefix = "memory:"
        items = {k[len(prefix) :]: v for k, v in (await store.all_settings()).items() if k.startswith(prefix)}
        return {"memory": items}

    async def forget(a: dict[str, Any]) -> Any:
        removed = await store.delete_setting(f"memory:{a.get('key')}")
        return {"ok": removed, "key": a.get("key")}

    async def list_schedules(_: dict[str, Any]) -> Any:
        return {"schedules": scheduler.list_schedules()}

    async def create_schedule(a: dict[str, Any]) -> Any:
        step = {"module": a.get("module"), "tool": a.get("tool"), "args": a.get("args") or {}}
        result = await scheduler.add_dynamic(
            str(a.get("cron") or ""),
            [step],
            description=a.get("description"),
            created_by="assistant",
        )
        if not result.get("ok"):
            raise BuiltinError(str(result.get("detail") or result.get("error")))
        return result

    async def delete_schedule(a: dict[str, Any]) -> Any:
        result = await scheduler.remove_dynamic(str(a.get("id") or ""))
        if not result.get("ok"):
            raise BuiltinError(str(result.get("detail") or result.get("error")))
        return result

    async def set_schedule_enabled(a: dict[str, Any]) -> Any:
        result = await scheduler.set_dynamic_enabled(str(a.get("id") or ""), bool(a.get("enabled")))
        if not result.get("ok"):
            raise BuiltinError(str(result.get("detail") or result.get("error")))
        return result

    async def _dashboard() -> list[dict[str, Any]]:
        cards = await store.get_setting(DASHBOARD_KEY)
        return [c for c in cards if isinstance(c, dict)] if isinstance(cards, list) else []

    async def list_cards(_: dict[str, Any]) -> Any:
        return {"cards": await _dashboard()}

    async def add_card(a: dict[str, Any]) -> Any:
        module = str(a.get("module") or "").strip()
        tool = str(a.get("tool") or "").strip()
        if not module or not tool:
            raise BuiltinError("module and tool are required")
        # A card reads through the owner read-tool path, which runs only tools a
        # module declares read. Refuse anything else up front, so a pinned card
        # cannot be a write and cannot be a tool that does not exist.
        mod = supervisor.modules.get(module)
        spec = mod.manifest.tools.get(tool) if (mod is not None and mod.manifest is not None) else None
        if spec is None:
            raise BuiltinError(f"{module}.{tool} is not an installed tool")
        if spec.cls != "read":
            raise BuiltinError(f"{module}.{tool} is not a read tool, so it cannot back a card")
        raw_args = a.get("args")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        title = str(a.get("title") or "").strip() or _default_title(module, tool, args)
        card = {
            "id": _card_id(module, tool, args),
            "type": "tool",
            "module": module,
            "tool": tool,
            "args": args,
            "title": title,
        }
        cards = [c for c in await _dashboard() if c.get("id") != card["id"]]
        cards.append(card)
        await store.set_setting(DASHBOARD_KEY, cards)
        return {"ok": True, "card": card}

    async def remove_card(a: dict[str, Any]) -> Any:
        card_id = a.get("id")
        module = a.get("module")
        tool = a.get("tool")

        def drop(c: dict[str, Any]) -> bool:
            if card_id and c.get("id") == card_id:
                return True
            return bool(module and tool and c.get("module") == module and c.get("tool") == tool)

        cards = await _dashboard()
        remaining = [c for c in cards if not drop(c)]
        await store.set_setting(DASHBOARD_KEY, remaining)
        return {"ok": len(remaining) < len(cards), "removed": len(cards) - len(remaining)}

    return {
        "list_locations": list_locations,
        "set_location": set_location,
        "delete_location": delete_location,
        "remember": remember,
        "recall": recall,
        "list_memory": list_memory,
        "forget": forget,
        "list_schedules": list_schedules,
        "create_schedule": create_schedule,
        "delete_schedule": delete_schedule,
        "set_schedule_enabled": set_schedule_enabled,
        "list_cards": list_cards,
        "add_card": add_card,
        "remove_card": remove_card,
    }


def build_core_module(store: Store, scheduler: Scheduler, supervisor: Supervisor) -> Module:
    """Assemble the synthetic `core` module: a manifest, a live tool list for the
    catalog, and an in-process client. It is inserted into the supervisor's module
    map and is ready from the start."""
    manifest = Manifest.model_validate(
        {
            "name": CORE_MODULE,
            "version": __version__,
            # Never spawned; the client dispatches in process. A placeholder argv
            # is required only to satisfy the manifest schema.
            "runtime": {"command": ["true"]},
            "tools": {name: {"class": cls} for name, cls, _desc, _schema in TOOL_DEFS},
        }
    )
    tools = [
        {"name": name, "description": desc, "inputSchema": schema} for name, _cls, desc, schema in TOOL_DEFS
    ]
    return Module(
        manifest=manifest,
        state=State.READY,
        client=BuiltinClient(_handlers(store, scheduler, supervisor)),  # type: ignore[arg-type]
        tools=tools,
    )
