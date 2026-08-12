"""The default: speech happens in the browser.

The client uses the Web Speech API to transcribe what it hears and to speak the
reply. The hub does neither, so voice works with no credentials configured, no
speech provider to pay for, and no recording of anyone's kitchen crossing the
network.

These adapters exist so the rest of the hub has one code path. They report that
the client handled it; they are not stubs waiting to be implemented.
"""

from __future__ import annotations

from .base import Synthesis, Transcription


class BrowserSTT:
    provider = "browser"

    async def transcribe(self, audio: bytes, mime: str) -> Transcription:
        # A client in this mode sends text, not audio. If audio arrives anyway
        # (an older page, a different client), it is dropped here: nothing is
        # written to disk and nothing is forwarded anywhere.
        return Transcription(
            provider=self.provider,
            handled_by_client=True,
            error=None,
        )

    async def aclose(self) -> None:
        return None


class BrowserTTS:
    provider = "browser"

    async def synthesize(self, text: str) -> Synthesis:
        return Synthesis(provider=self.provider, handled_by_client=True)

    async def aclose(self) -> None:
        return None
