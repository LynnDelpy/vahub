"""Server-side speech against an OpenAI-compatible API.

Transcription posts the audio to /audio/transcriptions (Whisper style) and
synthesis posts text to /audio/speech. Any service exposing those two routes
works, which is what makes a local Whisper or Piper server a drop-in
replacement for the hosted one.

Errors never propagate as exceptions and never carry the response body: a
provider error message can contain the request content, and occasionally the
credential that was rejected.
"""

from __future__ import annotations

import httpx

from .base import Synthesis, Transcription

# The API infers the codec from the filename, so the mime type has to be mapped
# to a plausible extension. Browsers record webm or ogg, hence the default.
_EXTENSIONS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp4": "mp4",
    "audio/flac": "flac",
}


def _client(base_url: str, api_key: str | None, timeout_s: float, **headers: str) -> httpx.AsyncClient:
    merged = dict(headers)
    if api_key:
        merged["authorization"] = f"Bearer {api_key}"
    return httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=merged, timeout=timeout_s)


def _error(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    return f"transport_error: {type(exc).__name__}"


class OpenAICompatSTT:
    provider = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        request_timeout_s: float = 60.0,
    ) -> None:
        self._model = model
        self._client = _client(base_url, api_key, request_timeout_s)

    async def transcribe(self, audio: bytes, mime: str) -> Transcription:
        suffix = _EXTENSIONS.get(mime.split(";")[0].strip().lower(), "webm")
        files = {"file": (f"audio.{suffix}", audio, mime)}
        try:
            resp = await self._client.post(
                "/audio/transcriptions", files=files, data={"model": self._model}
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as e:
            return Transcription(provider=self.provider, error=_error(e))
        except ValueError:  # a response that is not JSON at all
            return Transcription(provider=self.provider, error="bad_response")

        # The provider is not part of the trust boundary either: it can return
        # any shape, and the caller is promised a string.
        if not isinstance(payload, dict):
            return Transcription(provider=self.provider, error="bad_response")
        text = payload.get("text")
        if not isinstance(text, str):
            return Transcription(provider=self.provider, error="bad_response")
        return Transcription(text=text.strip(), provider=self.provider)

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenAICompatTTS:
    provider = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        voice: str,
        request_timeout_s: float = 60.0,
    ) -> None:
        self._model = model
        self._voice = voice
        self._client = _client(base_url, api_key, request_timeout_s, **{"content-type": "application/json"})

    async def synthesize(self, text: str) -> Synthesis:
        if not text.strip():
            return Synthesis(provider=self.provider)
        try:
            resp = await self._client.post(
                "/audio/speech",
                json={
                    "model": self._model,
                    "voice": self._voice,
                    "input": text,
                    "response_format": "mp3",
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            return Synthesis(provider=self.provider, error=_error(e))
        if not resp.content:
            return Synthesis(provider=self.provider, error="empty_audio")
        return Synthesis(audio=resp.content, mime="audio/mpeg", provider=self.provider)

    async def aclose(self) -> None:
        await self._client.aclose()
