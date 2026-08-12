"""LLM adapters: one interface, several backends (mock, OpenAI-compatible, Anthropic)."""

from .base import LLMAdapter, LLMError, LLMResult, ToolCall, ToolSpec, build_adapter

__all__ = ["LLMAdapter", "LLMError", "LLMResult", "ToolCall", "ToolSpec", "build_adapter"]
