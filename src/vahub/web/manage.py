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

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..config.models import MODEL_FIELDS, MODEL_SECRETS
from . import auth as web_auth
from .security import check_origin

if TYPE_CHECKING:
    from ..core.runtime import Runtime

_NAME = r"^[a-z0-9][a-z0-9_.-]{0,39}$"
_KEY = r"^[a-z0-9][a-z0-9_.:-]{0,59}$"
_MODULE = r"^[a-z][a-z0-9_-]{0,63}$"
_TOOL = r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$"
_SECTION = r"^(llm|stt|tts)$"
_FIELD = r"^[a-z][a-z_]{0,39}$"


def _first_line(text: str) -> str:
    """A validation error is a paragraph; a form field needs a sentence."""
    return str(text).strip().split("\n")[0][:200]


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


class ModelBody(BaseModel):
    """One model section as the UI submits it. Every field is optional: the form
    sends what it changed, and an omitted field keeps whatever is stored (or the
    config file's value, if nothing is stored)."""

    model_config = ConfigDict(extra="forbid")
    provider: str | None = Field(default=None, max_length=40)
    base_url: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=120)
    voice: str | None = Field(default=None, max_length=60)
    api_key: str | None = Field(default=None, max_length=500)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=200_000)


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
        # Deleting is a modification of the namespace too, so the same guard the
        # PUT has: the preferences editor must not touch the assistant's `memory:`
        # namespace, which is managed only through its gated tools.
        if key.startswith("memory:"):
            return JSONResponse({"ok": False, "error": "reserved_key"}, status_code=400)
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

    @router.post("/control/{module}/{tool}")
    async def call_control_tool(
        body: ToolCallBody,
        request: Request,
        module: str = Path(pattern=_MODULE),
        tool: str = Path(pattern=_TOOL),
    ) -> JSONResponse:
        """Let the signed-in owner act on a module directly: pause what is
        playing, move it to another speaker, turn the volume down.

        This is the same owner path as the card above, one step wider: it also
        runs write-class tools. A destructive tool is still refused here, because
        those are the ones that must be confirmed out of band, and the assistant
        (which is gated) remains the only way to reach them. Origin-checked,
        login-guarded, and audited as the acting user, like every other write."""
        check_origin(request, rt.config)
        who = await web_auth.current_username(request, rt)
        result = await rt.moduleapi.call_read(
            module, tool, body.args, subject=who, timeout_s=body.timeout_s, allow_write=True
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    # --- which model answers, listens and speaks --------------------------
    # The config file remains the source of truth for everything else; these
    # routes write a small set of named fields into the database and rebuild the
    # adapters. An API key is written and never read back, exactly like a module
    # token. The assistant has no tool for any of this: changing the model is the
    # owner's decision, made in a browser, not something a conversation can do.
    @router.get("/models")
    async def get_models(request: Request) -> JSONResponse:
        await web_auth.require_admin(request, rt)
        effective = getattr(rt, "models", rt.config)
        stored = await rt.store.model_config()
        sections = {"llm": effective.llm, "stt": effective.speech.stt, "tts": effective.speech.tts}
        out: dict[str, Any] = {}
        for name, section in sections.items():
            values: dict[str, Any] = {}
            for field in MODEL_FIELDS[name]:
                if field in MODEL_SECRETS:
                    continue  # never leaves the hub
                values[field] = getattr(section, field, None)
            out[name] = {
                **values,
                # Which secrets have a value, and which fields the owner set here
                # rather than in the file, so the UI can offer to clear one.
                "secrets_set": [
                    f for f in MODEL_FIELDS[name] if f in MODEL_SECRETS and getattr(section, f, None)
                ],
                "overridden": sorted(stored.get(name, {})),
            }
        out["providers"] = {
            "llm": ["openai_compat", "anthropic", "mock"],
            "stt": ["browser", "openai_compat", "none"],
            "tts": ["browser", "openai_compat", "none"],
        }
        return JSONResponse(out)

    @router.put("/models/{section}")
    async def put_models(
        body: ModelBody, request: Request, section: str = Path(pattern=_SECTION)
    ) -> JSONResponse:
        check_origin(request, rt.config)
        await web_auth.require_admin(request, rt)
        allowed = MODEL_FIELDS.get(section)
        if allowed is None:
            return JSONResponse({"ok": False, "error": "unknown_section"}, status_code=404)
        submitted = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
        rejected = sorted(set(submitted) - set(allowed))
        if rejected:
            # e.g. a voice for the language model: refuse rather than store a
            # value that section will never read.
            return JSONResponse(
                {"ok": False, "error": "unknown_field", "detail": ", ".join(rejected)},
                status_code=400,
            )
        for key, value in submitted.items():
            await rt.store.set_model_config(section, key, str(value))
        # Try the new settings before reporting success: a provider the config
        # model refuses would otherwise be stored and silently ignored.
        try:
            await rt.effective_model_config()
        except Exception as e:
            for key in submitted:
                await rt.store.delete_model_config(section, key)
            return JSONResponse(
                {"ok": False, "error": "invalid", "detail": _first_line(str(e))}, status_code=400
            )
        await rt.apply_model_config()
        return JSONResponse({"ok": True, "section": section, "set": sorted(submitted)})

    @router.delete("/models/{section}/{key}")
    async def delete_models(
        request: Request, section: str = Path(pattern=_SECTION), key: str = Path(pattern=_FIELD)
    ) -> JSONResponse:
        """Drop one override, so the config file's value applies again."""
        check_origin(request, rt.config)
        await web_auth.require_admin(request, rt)
        if key not in MODEL_FIELDS.get(section, ()):
            return JSONResponse({"ok": False, "error": "unknown_field"}, status_code=400)
        removed = await rt.store.delete_model_config(section, key)
        await rt.apply_model_config()
        return JSONResponse({"ok": removed, "section": section, "key": key})

    @router.post("/models/llm/test")
    async def test_llm(request: Request) -> JSONResponse:
        """Ask the configured model to say one word, so a wrong key or a
        misspelled model name is a sentence here rather than a broken chat."""
        check_origin(request, rt.config)
        await web_auth.require_admin(request, rt)
        try:
            result = await asyncio.wait_for(
                rt.llm.complete([{"role": "user", "content": "Reply with the single word: ready"}], []),
                timeout=30,
            )
        except TimeoutError:
            return JSONResponse({"ok": False, "error": "timeout", "detail": "no answer within 30s"})
        except Exception as e:
            return JSONResponse({"ok": False, "error": "failed", "detail": _first_line(str(e))})
        text = (getattr(result, "text", "") or "").strip()
        models = getattr(rt, "models", rt.config)
        return JSONResponse(
            {"ok": True, "provider": models.llm.provider, "model": models.llm.model, "reply": text[:120]}
        )

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
