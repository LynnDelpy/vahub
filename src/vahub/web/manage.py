"""User-facing management: saved locations, preferences, and schedules.

These routes let a signed-in person edit their own data from the web UI. They are
guarded by the login (the require_login middleware) and origin-checked on every
write, exactly like editing the config would be. They deliberately do NOT touch
the policy or the accounts, and they do NOT go through the policy gate: the gate
governs what the AGENT and the scheduler may do to modules, not what an
authenticated owner may save. The AGENT reaches the same data through the gated
`core.*` tools instead.

A schedule created here still runs as principal `scheduler`, so it is bounded by
the scheduler's policy at run time no matter who created it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import auth as web_auth
from .security import check_origin

if TYPE_CHECKING:
    from ..core.runtime import Runtime

_NAME = r"^[a-z0-9][a-z0-9_.-]{0,39}$"
_KEY = r"^[a-z0-9][a-z0-9_.:-]{0,59}$"
_MODULE = r"^[a-z][a-z0-9_-]{0,63}$"
_TOOL = r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$"


class ToolCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(default=10.0, gt=0, le=60)


class LocationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=80)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    address: str | None = Field(default=None, max_length=200)


class SettingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any = None


class StepBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    module: str = Field(max_length=40)
    tool: str = Field(max_length=60)
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(default=10.0, gt=0, le=300)


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cron: str = Field(max_length=100)
    steps: list[StepBody] = Field(min_length=1, max_length=10)
    description: str | None = Field(default=None, max_length=120)


class EnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def build_router(rt: Runtime) -> APIRouter:
    router = APIRouter()

    # --- locations --------------------------------------------------------
    @router.get("/locations")
    async def list_locations(request: Request) -> JSONResponse:
        return JSONResponse({"locations": await rt.store.list_locations()})

    @router.put("/locations/{name}")
    async def put_location(
        body: LocationBody, request: Request, name: str = Path(pattern=_NAME)
    ) -> JSONResponse:
        check_origin(request, rt.config)
        await rt.store.upsert_location(
            name,
            label=body.label,
            latitude=body.latitude,
            longitude=body.longitude,
            address=body.address,
        )
        return JSONResponse({"ok": True, "name": name})

    @router.delete("/locations/{name}")
    async def delete_location(request: Request, name: str = Path(pattern=_NAME)) -> JSONResponse:
        check_origin(request, rt.config)
        return JSONResponse({"ok": await rt.store.delete_location(name)})

    # --- preferences ------------------------------------------------------
    @router.get("/settings")
    async def get_settings(request: Request) -> JSONResponse:
        alls = await rt.store.all_settings()
        prefs = {k: v for k, v in alls.items() if not k.startswith("memory:")}
        memory = {k[len("memory:") :]: v for k, v in alls.items() if k.startswith("memory:")}
        return JSONResponse({"settings": prefs, "memory": memory})

    @router.put("/settings/{key}")
    async def put_setting(body: SettingBody, request: Request, key: str = Path(pattern=_KEY)) -> JSONResponse:
        check_origin(request, rt.config)
        # `memory:` is the assistant's own namespace, managed through its gated
        # tools; the preferences editor must not write into it.
        if key.startswith("memory:"):
            return JSONResponse({"ok": False, "error": "reserved_key"}, status_code=400)
        await rt.store.set_setting(key, body.value)
        return JSONResponse({"ok": True, "key": key})

    @router.delete("/settings/{key}")
    async def delete_setting(request: Request, key: str = Path(pattern=_KEY)) -> JSONResponse:
        check_origin(request, rt.config)
        return JSONResponse({"ok": await rt.store.delete_setting(key)})

    # --- reading module data (for dashboard cards) ------------------------
    @router.post("/tools/{module}/{tool}")
    async def call_read_tool(
        body: ToolCallBody,
        request: Request,
        module: str = Path(pattern=_MODULE),
        tool: str = Path(pattern=_TOOL),
    ) -> JSONResponse:
        """Let the signed-in owner run a module's read-only tool directly, which
        is what backs the dashboard cards (unread mail, open PRs, and so on).

        It is origin-checked like any write even though it only reads, because it
        reaches a module and should not be triggerable cross-site. The gate is
        bypassed here on purpose (the owner is not the agent), but moduleapi
        restricts this path to read-class tools, so it can never be a write or a
        destructive action."""
        check_origin(request, rt.config)
        who = await web_auth.current_username(request, rt)
        result = await rt.moduleapi.call_read(module, tool, body.args, subject=who, timeout_s=body.timeout_s)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    # --- schedules --------------------------------------------------------
    @router.get("/schedules")
    async def list_schedules(request: Request) -> JSONResponse:
        return JSONResponse({"schedules": rt.scheduler.list_schedules()})

    @router.post("/schedules")
    async def create_schedule(body: ScheduleBody, request: Request) -> JSONResponse:
        check_origin(request, rt.config)
        who = await web_auth.current_username(request, rt)
        result = await rt.scheduler.add_dynamic(
            body.cron,
            [step.model_dump() for step in body.steps],
            description=body.description,
            created_by=who,
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @router.delete("/schedules/{schedule_id}")
    async def delete_schedule(request: Request, schedule_id: str = Path(pattern=_NAME)) -> JSONResponse:
        check_origin(request, rt.config)
        result = await rt.scheduler.remove_dynamic(schedule_id)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @router.post("/schedules/{schedule_id}/enabled")
    async def set_enabled(
        body: EnabledBody, request: Request, schedule_id: str = Path(pattern=_NAME)
    ) -> JSONResponse:
        check_origin(request, rt.config)
        result = await rt.scheduler.set_dynamic_enabled(schedule_id, body.enabled)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    return router


__all__ = ["build_router"]
