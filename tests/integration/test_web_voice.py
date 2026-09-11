"""The voice endpoint, which is the only route that takes a body from a browser
worth more than a few kilobytes and hands it to a third party.

Two decisions are being checked here.

Every way speech can fail to happen is an ordinary JSON answer, never a 500: a
provider that is switched off, a browser that already did the work, a
transcription API having a bad day, and a silence that transcribed to nothing.
The reply still stands when only the reading-aloud fails.

And the size limit is enforced twice, because the two checks catch different
lies: the declared `Content-Length` is refused before a byte is read, and the
read itself stops at the limit for a body that never declared one.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException, UploadFile

from vahub.config.models import Config
from vahub.speech.base import Synthesis, Transcription
from vahub.web.api import _as_dict, _read_bounded
from vahub.web.app import create_app

pytestmark = pytest.mark.integration

ALLOWED_ORIGIN = "http://localhost:8080"
HEADERS = {"origin": ALLOWED_ORIGIN}
AUDIO = {"audio": ("turn.webm", b"not really audio", "audio/webm")}


def make_config(state_dir: Path, modules_dir: Path, stt_provider: str) -> Config:
    return Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "web": {"origin_allowlist": [ALLOWED_ORIGIN], "auth": {"enabled": False}},
            "llm": {"provider": "mock"},
            "speech": {"stt": {"provider": stt_provider}},
            "policy": {"default": "deny", "rules": {}},
        }
    )


class FakeSTT:
    """Speech adapters return failures as values, so a fake only has to hand
    back the Transcription the test is about."""

    provider = "openai_compat"

    def __init__(self, result: Transcription) -> None:
        self.result = result
        self.seen: list[tuple[bytes, str]] = []

    async def transcribe(self, audio: bytes, mime: str) -> Transcription:
        self.seen.append((audio, mime))
        return self.result

    async def aclose(self) -> None:
        return None


class FakeTTS:
    provider = "openai_compat"

    def __init__(self, result: Synthesis) -> None:
        self.result = result

    async def synthesize(self, text: str) -> Synthesis:
        return self.result

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def runtime(construct, state_dir: Path, modules_dir: Path, request):
    from vahub.core.runtime import Runtime

    provider = getattr(request, "param", "openai_compat")
    config = make_config(state_dir, modules_dir, provider)
    rt = construct(Runtime, config=config, config_path=modules_dir.parent / "vahub.yaml")
    await rt.store.open()
    rt.supervisor.discover()
    try:
        yield rt
    finally:
        await rt.supervisor.stop()
        await rt.store.close()


@pytest.fixture
async def client(runtime):
    transport = httpx.ASGITransport(app=create_app(runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def post_voice(client, **kwargs: Any) -> httpx.Response:
    return await client.post("/api/voice", files=AUDIO, headers=HEADERS, **kwargs)


# --------------------------------------------------------------------------
# nothing to send the audio to
# --------------------------------------------------------------------------
@pytest.mark.parametrize("runtime", ["browser"], indirect=True)
async def test_voice_says_which_endpoint_to_use_when_no_server_model_is_configured(client) -> None:
    # The default install has no credentials, so this is the common case rather
    # than an edge one: answer it plainly instead of erroring.
    response = await post_voice(client)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "no_server_stt"


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------
async def test_voice_refuses_an_upload_that_declares_itself_too_large(client, runtime, monkeypatch) -> None:
    # Both size checks answer 413, so status alone cannot tell them apart.
    # Spying on the reader is what pins this one: it refuses on the declared
    # length, without the body being read at all.
    reads: list[int] = []

    async def spy(upload: UploadFile, limit: int) -> bytes:
        reads.append(limit)
        return b""

    monkeypatch.setattr("vahub.web.api.MAX_AUDIO_BYTES", 8)
    monkeypatch.setattr("vahub.web.api._read_bounded", spy)
    runtime.stt = FakeSTT(Transcription(text="never reached"))

    response = await post_voice(client)

    assert response.status_code == 413
    assert reads == []
    assert runtime.stt.seen == []


async def test_read_bounded_stops_at_the_limit_rather_than_buffering_the_whole_body() -> None:
    # A chunked body declares no length, so this is the check that catches it.
    upload = UploadFile(filename="turn.webm", file=io.BytesIO(b"x" * 4096))

    with pytest.raises(HTTPException) as raised:
        await _read_bounded(upload, 1024)

    assert raised.value.status_code == 413


async def test_read_bounded_returns_a_body_that_fits() -> None:
    upload = UploadFile(filename="turn.webm", file=io.BytesIO(b"x" * 512))

    assert await _read_bounded(upload, 1024) == b"x" * 512


# --------------------------------------------------------------------------
# transcription outcomes
# --------------------------------------------------------------------------
async def test_voice_sends_the_client_to_chat_when_the_browser_already_transcribed(client, runtime) -> None:
    runtime.stt = FakeSTT(Transcription(provider="browser", handled_by_client=True))

    response = await post_voice(client)

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "client_side_stt"
    assert body["provider"] == "browser"


async def test_voice_reports_a_transcription_failure_as_a_bad_gateway(client, runtime) -> None:
    runtime.stt = FakeSTT(Transcription(error="upstream refused the key"))

    response = await post_voice(client)

    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "stt_failed"
    assert body["detail"] == "upstream refused the key"


async def test_voice_answers_plainly_when_nothing_was_said(client, runtime) -> None:
    # Whitespace only: a silent recording is not a failure, and running an agent
    # turn on an empty string would be.
    runtime.stt = FakeSTT(Transcription(text="   "))

    response = await post_voice(client)

    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "empty_transcript"}


# --------------------------------------------------------------------------
# a whole spoken turn
# --------------------------------------------------------------------------
async def test_voice_returns_the_transcript_and_the_spoken_reply(client, runtime) -> None:
    runtime.stt = FakeSTT(Transcription(text="  what time is it  "))
    runtime.tts = FakeTTS(Synthesis(audio=b"spoken-bytes", mime="audio/mpeg"))

    response = await post_voice(client)

    assert response.status_code == 200
    body = response.json()
    # The transcript is stripped before it becomes the turn.
    assert body["transcript"] == "what time is it"
    assert base64.b64decode(body["audio"]) == b"spoken-bytes"
    assert body["audio_mime"] == "audio/mpeg"
    assert runtime.stt.seen == [(b"not really audio", "audio/webm")]


async def test_voice_still_answers_when_only_the_reading_aloud_fails(client, runtime) -> None:
    runtime.stt = FakeSTT(Transcription(text="hello"))
    runtime.tts = FakeTTS(Synthesis(error="voice model unavailable"))

    response = await post_voice(client)

    assert response.status_code == 200
    body = response.json()
    assert body["transcript"] == "hello"
    assert body["tts_error"] == "voice model unavailable"
    assert "audio" not in body
    # The answer itself is unaffected by a silent speaker.
    assert body.get("reply")


# --------------------------------------------------------------------------
# untrusted shapes
# --------------------------------------------------------------------------
def test_as_dict_passes_an_object_through() -> None:
    assert _as_dict({"reply": "hi"}, "agent_error") == {"reply": "hi"}


def test_as_dict_turns_an_unexpected_shape_into_a_dull_value() -> None:
    # Once a module is in the picture the turn can hand back anything. A caller
    # of this API gets an object or nothing, never a 500.
    assert _as_dict(["not", "a", "dict"], "agent_error") == {
        "ok": False,
        "error": "agent_error",
        "detail": "['not', 'a', 'dict']",
    }
