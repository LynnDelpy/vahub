"""The LLM adapter interface and its factory.

The agent loop speaks one shape to every backend: a list of OpenAI-style chat
messages plus the tools that are currently allowed, and back either text (the
final answer) or one or more tool calls. Keeping the interface this narrow is
what makes the backend swappable, and it is why the conversation is stored in
one canonical format instead of a provider's format (the Anthropic adapter
translates on the way out and back).

Adapters normalise usage to include `total_tokens`, because the loop enforces a
token budget and must not have to know which provider reported what.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ...config.models import LLMConfig


class LLMError(Exception):
    """A backend call failed. The message is shown to the user, so it stays
    short and carries the provider's own wording rather than a stack trace."""


@dataclass(slots=True)
class ToolSpec:
    """A tool as offered to the model.

    `name` is the sanitized function name the model sees; `module` and `tool` are
    how the loop routes the call back through the policy gate to the module. The
    model never gets to name a module directly."""

    name: str
    module: str
    tool: str
    description: str | None = None
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class LLMResult:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] | None = None
    stop_reason: str | None = None


class LLMAdapter(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMResult: ...

    async def aclose(self) -> None: ...


def build_adapter(cfg: LLMConfig) -> LLMAdapter:
    """Construct the adapter named by the config. Imports are local so a hub
    running the mock backend never needs an HTTP client loaded."""
    if cfg.provider == "mock":
        from .mock import MockLLM

        return MockLLM()
    if cfg.provider == "openai_compat":
        from .openai_compat import OpenAICompatLLM

        return OpenAICompatLLM(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            request_timeout_s=cfg.request_timeout_s,
        )
    if cfg.provider == "anthropic":
        from .anthropic import AnthropicLLM

        return AnthropicLLM(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            request_timeout_s=cfg.request_timeout_s,
        )
    raise ValueError(f"unknown llm provider: {cfg.provider!r}")


def normalise_usage(raw: Any) -> dict[str, int] | None:
    """Reduce a provider's usage block to plain ints, adding `total_tokens` when
    the provider only reports the two halves. Providers have been known to send
    nulls here, and a budget must not be skipped because of one."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        out[key] = int(value)
    if "total_tokens" not in out:
        prompt = out.get("prompt_tokens", out.get("input_tokens", 0))
        completion = out.get("completion_tokens", out.get("output_tokens", 0))
        if prompt or completion:
            out["total_tokens"] = prompt + completion
    return out or None
