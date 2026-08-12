"""The REST surface: what the console and any script drive the hub with.

Two properties hold everywhere in this file.

* Every input is bounded before it reaches the rest of the hub (message length,
  argument size, timeout range, upload size), so an unauthenticated request on a
  LAN cannot turn into unbounded memory or an unbounded tool call.
* Nothing a module produced is trusted. A module's tool list, health payload and
  stderr are shaped by code the hub did not write, so a non-dict where a dict was
  expected must produce a dull JSON value, never a 500 and never a crash.

Authentication is not done here. It belongs to the reverse proxy in front of the
hub; the only thing the proxy tells us is the subject, which is recorded in the
audit log and is never an authorization input.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, File, Form, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .security import auth_subject, check_origin

if TYPE_CHECKING:
    from ..core.runtime import Runtime

# Bounds. They are deliberately generous for a human at a console and still small
# enough that a hostile client cannot make the hub allocate without limit.
MAX_MESSAGE_CHARS = 8_000
MAX_ARGS_BYTES = 8_192
MAX_TIMEOUT_S = 60.0
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_LOG_LINES = 500
MAX_AUDIT_ROWS = 500

_MODULE_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
_TOOL_NAME_PATTERN = r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$"
_SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_PENDING_ID_PATTERN = r"^[0-9a-fA-F]{8,64}$"


class ChatTurn(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    session_id: str | None = Field(default=None, pattern=_SESSION_ID_PATTERN)


class DevCall(BaseModel):
    module: str = Field(pattern=_MODULE_NAME_PATTERN)
    tool: str = Field(pattern=_TOOL_NAME_PATTERN)
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(10.0, gt=0, le=MAX_TIMEOUT_S)


def state_value(state: Any) -> str:
    # The supervisor's state is an enum whose value is the string the API and the
    # console use; accept a plain string too so the view never depends on it.
    return str(getattr(state, "value", state) or "")


def module_view(mod: Any) -> dict[str, Any]:
    """The public shape of one module. Shared with the WebSocket snapshot."""
    manifest = mod.manifest
    tools = [t.get("name") for t in mod.tools if isinstance(t, dict)]
    return {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "state": state_value(mod.state),
        "last_error": mod.last_error,
        "health": mod.health if isinstance(mod.health, dict) else {"raw": mod.health},
        "tools": [str(name) for name in tools if isinstance(name, str)],
        "restarts": mod.restarts,
    }


def _as_dict(result: Any, fallback_error: str) -> dict[str, Any]:
    """Anything downstream may hand back an unexpected shape once a module is in
    the picture. Callers of this API get an object or nothing."""
    if isinstance(result, dict):
        return result
    return {"ok": False, "error": fallback_error, "detail": str(result)[:500]}


async def _read_bounded(upload: UploadFile, limit: int) -> bytes:
    """Read an upload in chunks, refusing it as soon as it passes the limit
    rather than after the whole body has been buffered."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="audio too large")
        chunks.append(chunk)
    return b"".join(chunks)


def build_router(rt: Runtime) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/client-config")
    async def client_config() -> JSONResponse:
        # The browser needs this to choose a voice path: "browser" means it
        # transcribes and speaks locally (no audio leaves the machine), while
        # "openai_compat" means it posts audio to /api/voice.
        speech = rt.config.speech
        return JSONResponse(
            {
                "stt_provider": speech.stt.provider,
                "tts_provider": speech.tts.provider,
                "dev_tools_endpoint": rt.config.web.dev_tools_endpoint,
                "timezone": rt.config.hub.timezone,
            }
        )

    @router.get("/modules")
    async def modules() -> JSONResponse:
        return JSONResponse([module_view(m) for m in rt.supervisor.modules.values()])

    @router.get("/modules/{name}/logs")
    async def module_logs(
        name: str = Path(pattern=_MODULE_NAME_PATTERN),
        limit: int = Query(200, ge=1, le=MAX_LOG_LINES),
    ) -> JSONResponse:
        mod = rt.supervisor.modules.get(name)
        if mod is None:
            raise HTTPException(status_code=404, detail="unknown module")
        lines = [str(line) for line in mod.stderr_ring][-limit:]
        return JSONResponse({"module": name, "lines": lines})

    @router.get("/tools")
    async def tools() -> JSONResponse:
        return JSONResponse(rt.registry.list_tools())

    @router.post("/chat")
    async def chat(turn: ChatTurn, request: Request) -> JSONResponse:
        check_origin(request, rt.config)
        session = rt.sessions.get_or_create(turn.session_id)
        result = _as_dict(await rt.agent.run_turn(session, turn.message), "agent_error")
        rt.sessions.trim(session)
        return JSONResponse(result)

    @router.post("/voice")
    async def voice(
        request: Request,
        audio: UploadFile = File(...),
        session_id: str | None = Form(default=None, pattern=_SESSION_ID_PATTERN),
    ) -> JSONResponse:
        check_origin(request, rt.config)
        if rt.config.speech.stt.provider != "openai_compat":
            # No server-side model is configured, so there is nothing to send the
            # audio to. The console falls back to the browser's own recognition.
            return JSONResponse(
                {
                    "ok": False,
                    "error": "no_server_stt",
                    "detail": "speech.stt.provider is not openai_compat; the browser transcribes locally",
                }
            )
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="audio too large")

        data = await _read_bounded(audio, MAX_AUDIO_BYTES)
        heard = await rt.stt.transcribe(data, audio.content_type or "audio/webm")
        if heard.handled_by_client:
            # The browser transcribes locally in this configuration, so audio
            # posted here has nowhere to go. Say which endpoint to use instead.
            return JSONResponse(
                {"ok": False, "error": "client_side_stt", "provider": heard.provider,
                 "detail": "speech.stt.provider is 'browser'; send the text to /api/chat"},
                status_code=409,
            )
        if heard.error:
            return JSONResponse({"ok": False, "error": "stt_failed", "detail": heard.error}, 502)
        transcript = heard.text.strip()
        if not transcript:
            return JSONResponse({"ok": False, "error": "empty_transcript"})

        session = rt.sessions.get_or_create(session_id)
        result = _as_dict(await rt.agent.run_turn(session, transcript), "agent_error")
        rt.sessions.trim(session)
        payload: dict[str, Any] = {"transcript": transcript, **result}

        spoken = await rt.tts.synthesize(str(result.get("reply") or ""))
        if spoken.audio is not None:
            payload["audio"] = base64.b64encode(spoken.audio).decode("ascii")
            payload["audio_mime"] = spoken.mime or "audio/mpeg"
        elif spoken.error:
            # The answer still stands even when it cannot be read aloud.
            payload["tts_error"] = spoken.error
        return JSONResponse(payload)

    @router.get("/pending")
    async def pending() -> JSONResponse:
        return JSONResponse(await rt.store.list_pending())

    @router.post("/confirm/{pending_id}")
    async def confirm(
        request: Request,
        pending_id: str = Path(pattern=_PENDING_ID_PATTERN),
    ) -> JSONResponse:
        # Confirming runs the arguments frozen when the call was gated, not
        # whatever the model may have produced since.
        check_origin(request, rt.config)
        subject = auth_subject(request, rt.config)
        result = await rt.moduleapi.confirm(pending_id, subject=subject)
        return JSONResponse(_as_dict(result, "confirm_error"))

    @router.get("/audit")
    async def audit(limit: int = Query(200, ge=1, le=MAX_AUDIT_ROWS)) -> JSONResponse:
        return JSONResponse(await rt.store.recent_tool_calls(limit=limit))

    @router.get("/schedules")
    async def schedules() -> JSONResponse:
        return JSONResponse(rt.scheduler.list_schedules())

    @router.post("/schedules/{schedule_id}/run")
    async def run_schedule(
        request: Request,
        schedule_id: str = Path(max_length=64),
    ) -> JSONResponse:
        # A routine runs with the scheduler principal, which is usually allowed to
        # act unattended. Triggering that from an unauthenticated request is a
        # development affordance, so it shares the dev endpoint's switch.
        if not rt.config.web.dev_tools_endpoint:
            raise HTTPException(status_code=403, detail="dev tools endpoint disabled")
        check_origin(request, rt.config)
        result = await rt.scheduler.run_schedule(schedule_id)
        return JSONResponse(_as_dict(result, "schedule_error"))

    @router.post("/dev/call")
    async def dev_call(call: DevCall, request: Request) -> JSONResponse:
        # Calls one tool without the agent. The policy gate still applies (the
        # call goes through the same ModuleAPI as everything else), but the route
        # is unauthenticated, so it stays off unless it was turned on.
        if not rt.config.web.dev_tools_endpoint:
            raise HTTPException(status_code=403, detail="dev tools endpoint disabled")
        check_origin(request, rt.config)
        if len(json.dumps(call.args, default=str)) > MAX_ARGS_BYTES:
            raise HTTPException(status_code=413, detail="args too large")
        result = await rt.moduleapi.call(
            module=call.module,
            tool=call.tool,
            args=call.args,
            timeout_s=call.timeout_s,
            principal="dev",
        )
        return JSONResponse(_as_dict(result, "call_error"))

    return router


__all__ = ["build_router", "module_view", "state_value"]
