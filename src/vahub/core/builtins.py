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

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..__about__ import __version__
from ..contracts.manifest import Manifest
from .supervisor import Module, State

if TYPE_CHECKING:
    from ..scheduler import Scheduler
    from ..storage.store import Store

CORE_MODULE = "core"

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
}


def _handlers(store: Store, scheduler: Scheduler) -> dict[str, Handler]:
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
    }


def build_core_module(store: Store, scheduler: Scheduler) -> Module:
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
        client=BuiltinClient(_handlers(store, scheduler)),  # type: ignore[arg-type]
        tools=tools,
    )
