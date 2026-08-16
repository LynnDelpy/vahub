"""The REST surface, which is the assistant and nothing else.

Asking something, speaking something, and approving an action that was held back
for a person. There is deliberately no way here to read module states, module
stderr, the tool catalogue or the audit log, and no way to invoke a tool
directly: those are for whoever runs the service, who has the CLI and the
service log. A page that may be handed to someone who just wants to ask a
question should not also be a debugger.

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
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, File, Form, HTTPException, Path, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .security import auth_subject, check_origin

if TYPE_CHECKING:
    from ..core.runtime import Runtime

# Bounds. They are deliberately generous for a human at a console and still small
# enough that a hostile client cannot make the hub allocate without limit.
MAX_MESSAGE_CHARS = 8_000
MAX_AUDIO_BYTES = 8 * 1024 * 1024

_SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_PENDING_ID_PATTERN = r"^[0-9a-fA-F]{8,64}$"


class ChatTurn(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    session_id: str | None = Field(default=None, pattern=_SESSION_ID_PATTERN)


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
                "timezone": rt.config.hub.timezone,
            }
        )

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
            # audio to. The page falls back to the browser's own recognition.
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

    return router


__all__ = ["build_router"]
