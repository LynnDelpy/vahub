"""OpenAI-compatible Chat Completions adapter.

Works against any endpoint speaking the OpenAI `/chat/completions` shape:
OpenAI, OpenRouter, Groq, Together, a local Ollama or llama.cpp server. The
canonical message format the loop keeps is already this one, so nothing is
translated here.

The parsing is deliberately defensive. A tool-call argument string is model
output, not a contract: it can be truncated, empty, or a JSON scalar instead of
an object, and none of those may raise inside the agent loop.
"""

from __future__ import annotations

import json
from typing import Any

from .base import LLMError, LLMResult, ToolCall, ToolSpec, normalise_usage


class OpenAICompatLLM:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        request_timeout_s: float = 60.0,
    ) -> None:
        import httpx  # local import: the mock backend must not need an HTTP client

        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=request_timeout_s,
        )

    async def complete(self, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMResult:
        import httpx

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description or "",
                        "parameters": spec.parameters or {"type": "object", "properties": {}},
                    },
                }
                for spec in tools
            ]
            payload["tool_choice"] = "auto"

        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as e:
            raise LLMError(f"cannot reach the model endpoint: {e}") from e
        if response.status_code >= 400:
            raise LLMError(f"model endpoint returned {response.status_code}: {_error_text(response)}")

        try:
            body = response.json()
        except ValueError as e:
            raise LLMError("model endpoint returned a body that is not JSON") from e
        if not isinstance(body, dict):
            raise LLMError("model endpoint returned an unexpected body")

        choices = body.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            message = {}

        content = message.get("content")
        return LLMResult(
            text=content if isinstance(content, str) else None,
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
            usage=normalise_usage(body.get("usage")),
            stop_reason=choice.get("finish_reason") if isinstance(choice, dict) else None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_tool_calls(raw: Any) -> list[ToolCall]:
    if not isinstance(raw, list):
        return []
    calls: list[ToolCall] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{index}"),
                name=name,
                arguments=_parse_arguments(function.get("arguments")),
            )
        )
    return calls


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Arguments arrive as a JSON string. Anything that is not an object becomes
    an empty one, and the policy gate then rejects the call on its merits rather
    than the loop dying on a malformed generation."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _error_text(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:300]
        if isinstance(error, str):
            return error[:300]
    return str(body)[:300]
