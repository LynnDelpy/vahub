"""The speech adapters, with the HTTP call mocked.

The provider is not trusted: it can return any shape, and the caller is promised
a Transcription/Synthesis, never an exception. These feed the openai_compat
adapters canned responses through an httpx MockTransport, and check the browser
adapters do no network at all.
"""

from __future__ import annotations

from typing import Any

import httpx

from vahub.speech.browser import BrowserSTT, BrowserTTS
from vahub.speech.openai_compat import OpenAICompatSTT, OpenAICompatTTS


def _mock(adapter: Any, responder) -> None:
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(responder), base_url="http://mock")


def _stt() -> OpenAICompatSTT:
    return OpenAICompatSTT(base_url="http://mock", api_key="k", model="whisper-1")


def _tts() -> OpenAICompatTTS:
    return OpenAICompatTTS(base_url="http://mock", api_key="k", model="tts-1", voice="alloy")


# --------------------------------------------------------------------------
# STT
# --------------------------------------------------------------------------
async def test_stt_success() -> None:
    adapter = _stt()
    _mock(adapter, lambda r: httpx.Response(200, json={"text": "  hello world  "}))
    heard = await adapter.transcribe(b"audio", "audio/webm")
    assert heard.text == "hello world" and heard.error is None


async def test_stt_http_error_is_reported_not_raised() -> None:
    adapter = _stt()
    _mock(adapter, lambda r: httpx.Response(502, text="upstream down"))
    heard = await adapter.transcribe(b"audio", "audio/webm")
    assert heard.error and not heard.text


async def test_stt_non_json_body() -> None:
    adapter = _stt()
    _mock(adapter, lambda r: httpx.Response(200, text="not json"))
    heard = await adapter.transcribe(b"audio", "audio/webm")
    assert heard.error == "bad_response"


async def test_stt_unexpected_shape() -> None:
    adapter = _stt()
    _mock(adapter, lambda r: httpx.Response(200, json={"no_text_here": 1}))
    heard = await adapter.transcribe(b"audio", "audio/webm")
    assert heard.error == "bad_response"


# --------------------------------------------------------------------------
# TTS
# --------------------------------------------------------------------------
async def test_tts_success_returns_audio() -> None:
    adapter = _tts()
    _mock(adapter, lambda r: httpx.Response(200, content=b"MP3BYTES"))
    spoken = await adapter.synthesize("hello")
    assert spoken.audio == b"MP3BYTES" and spoken.mime == "audio/mpeg"


async def test_tts_empty_text_makes_no_request() -> None:
    adapter = _tts()

    def explode(_r):  # must not be called
        raise AssertionError("no request should be made for empty text")

    _mock(adapter, explode)
    spoken = await adapter.synthesize("   ")
    assert spoken.audio is None and spoken.error is None


async def test_tts_http_error() -> None:
    adapter = _tts()
    _mock(adapter, lambda r: httpx.Response(500, text="boom"))
    spoken = await adapter.synthesize("hello")
    assert spoken.error and spoken.audio is None


async def test_tts_empty_content_is_an_error() -> None:
    adapter = _tts()
    _mock(adapter, lambda r: httpx.Response(200, content=b""))
    spoken = await adapter.synthesize("hello")
    assert spoken.error == "empty_audio"


# --------------------------------------------------------------------------
# browser: no network
# --------------------------------------------------------------------------
async def test_browser_stt_reports_client_handled() -> None:
    heard = await BrowserSTT().transcribe(b"audio", "audio/webm")
    assert heard.handled_by_client is True and heard.error is None


async def test_browser_tts_reports_client_handled() -> None:
    spoken = await BrowserTTS().synthesize("hello")
    assert spoken.handled_by_client is True and spoken.audio is None
