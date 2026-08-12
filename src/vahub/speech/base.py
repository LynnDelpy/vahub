"""Speech adapters: the interface the rest of the hub programs against.

The hub never talks to a speech provider directly. It calls
`transcribe(audio, mime)` and `synthesize(text)` and gets a value back.

Two decisions.

Failures are values, not exceptions. An unreachable transcription API, a
disabled provider, or work the browser already did are all ordinary returns, so
a speech backend having a bad day cannot take a request handler down with it.

The default provider is the browser. The client transcribes and speaks locally,
which means voice works with no credentials configured and no audio leaves the
machine. Server-side speech is opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config.models import Config, SpeechConfig, STTConfig, TTSConfig


@dataclass(frozen=True, slots=True)
class Transcription:
    """The outcome of one transcription attempt."""

    text: str = ""
    provider: str = "none"
    # The client transcribed it itself; `text` is empty and that is not an error.
    handled_by_client: bool = False
    # Short and safe to show a user or write to a log. Never a response body:
    # provider errors echo request content and sometimes credentials.
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Synthesis:
    """The outcome of one synthesis attempt. `audio` is None when there is
    nothing for the server to send, either because the client speaks the reply
    itself or because synthesis failed."""

    audio: bytes | None = None
    mime: str | None = None
    provider: str = "none"
    handled_by_client: bool = False
    error: str | None = None


class STTAdapter(Protocol):
    provider: str

    async def transcribe(self, audio: bytes, mime: str) -> Transcription: ...

    async def aclose(self) -> None: ...


class TTSAdapter(Protocol):
    provider: str

    async def synthesize(self, text: str) -> Synthesis: ...

    async def aclose(self) -> None: ...


class DisabledSTT:
    """provider=none. Speech input is off; say so rather than pretending."""

    provider = "none"

    async def transcribe(self, audio: bytes, mime: str) -> Transcription:
        return Transcription(provider=self.provider, error="stt_disabled")

    async def aclose(self) -> None:
        return None


class DisabledTTS:
    provider = "none"

    async def synthesize(self, text: str) -> Synthesis:
        return Synthesis(provider=self.provider, error="tts_disabled")

    async def aclose(self) -> None:
        return None


def build_stt(config: Config | SpeechConfig | STTConfig) -> STTAdapter:
    cfg = _stt_config(config)
    if cfg.provider == "browser":
        from .browser import BrowserSTT

        return BrowserSTT()
    if cfg.provider == "openai_compat":
        from .openai_compat import OpenAICompatSTT

        return OpenAICompatSTT(cfg.base_url, cfg.api_key, cfg.model, cfg.request_timeout_s)
    if cfg.provider == "none":
        return DisabledSTT()
    raise ValueError(f"unknown stt provider: {cfg.provider!r}")


def build_tts(config: Config | SpeechConfig | TTSConfig) -> TTSAdapter:
    cfg = _tts_config(config)
    if cfg.provider == "browser":
        from .browser import BrowserTTS

        return BrowserTTS()
    if cfg.provider == "openai_compat":
        from .openai_compat import OpenAICompatTTS

        return OpenAICompatTTS(cfg.base_url, cfg.api_key, cfg.model, cfg.voice, cfg.request_timeout_s)
    if cfg.provider == "none":
        return DisabledTTS()
    raise ValueError(f"unknown tts provider: {cfg.provider!r}")


# The factories accept whichever object the caller happens to hold. Callers with
# the whole Config should not have to remember the attribute chain, and a test
# building one STTConfig should not have to wrap it in two containers.
def _stt_config(config: Config | SpeechConfig | STTConfig) -> STTConfig:
    if isinstance(config, Config):
        return config.speech.stt
    if isinstance(config, SpeechConfig):
        return config.stt
    return config


def _tts_config(config: Config | SpeechConfig | TTSConfig) -> TTSConfig:
    if isinstance(config, Config):
        return config.speech.tts
    if isinstance(config, SpeechConfig):
        return config.tts
    return config
