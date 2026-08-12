"""Speech input and output. The browser does it by default."""

from .base import STTAdapter, Synthesis, Transcription, TTSAdapter, build_stt, build_tts

__all__ = [
    "STTAdapter",
    "Synthesis",
    "TTSAdapter",
    "Transcription",
    "build_stt",
    "build_tts",
]
