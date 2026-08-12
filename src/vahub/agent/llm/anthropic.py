"""Anthropic Messages API adapter (native, not the OpenAI compatibility shim).

The compatibility endpoint exists, but it hides the parts of the API that matter
here: tool_use and tool_result are content blocks, the system prompt is a
top-level field rather than a message, and usage is reported per direction.
Talking to the real API keeps those explicit.

Three translation details that are easy to get wrong:

* System messages are lifted out of the conversation and joined into the
  top-level `system` field. Anthropic rejects role="system" inside `messages`.
* A tool result is a *user* message containing a `tool_result` block, so the
  results of several parallel tool calls are merged into one user message.
  Adjacent same-role messages are merged for the same reason.
* A message with empty content is rejected by the API, so an assistant turn that
  carried neither text nor a tool call is dropped rather than sent.
"""

from __future__ import annotations

import json
from typing import Any

from .base import LLMError, LLMResult, ToolCall, ToolSpec, normalise_usage

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicLLM:
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
        root = (base_url or "https://api.anthropic.com").rstrip("/")
        # The config default points at an OpenAI-style base URL, so accept a URL
        # that already ends in /v1 instead of producing /v1/v1/messages.
        self._path = "/messages" if root.endswith("/v1") else "/v1/messages"
        headers = {
            "content-type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if api_key:
            headers["x-api-key"] = api_key
        self._client = httpx.AsyncClient(base_url=root, headers=headers, timeout=request_timeout_s)

    async def complete(self, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMResult:
        import httpx

        system, converted = _convert_messages(messages)
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "messages": converted,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": spec.name,
                    "description": spec.description or "",
                    "input_schema": _input_schema(spec.parameters),
                }
                for spec in tools
            ]

        try:
            response = await self._client.post(self._path, json=payload)
        except httpx.HTTPError as e:
            raise LLMError(f"cannot reach the Anthropic API: {e}") from e
        if response.status_code >= 400:
            raise LLMError(f"Anthropic API returned {response.status_code}: {_error_text(response)}")

        try:
            body = response.json()
        except ValueError as e:
            raise LLMError("Anthropic API returned a body that is not JSON") from e
        if not isinstance(body, dict):
            raise LLMError("Anthropic API returned an unexpected body")

        texts: list[str] = []
        calls: list[ToolCall] = []
        blocks = body.get("content")
        for index, block in enumerate(blocks if isinstance(blocks, list) else []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                arguments = block.get("input")
                calls.append(
                    ToolCall(
                        id=str(block.get("id") or f"call_{index}"),
                        name=block["name"],
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )

        return LLMResult(
            text="\n".join(texts) or None,
            tool_calls=calls,
            usage=normalise_usage(body.get("usage")),
            stop_reason=body.get("stop_reason") if isinstance(body.get("stop_reason"), str) else None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                system_parts.append(content)
            continue

        if role == "tool":
            _append(
                out,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(message.get("tool_call_id") or ""),
                        "content": _as_text(message.get("content")),
                    }
                ],
            )
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = message.get("content")
            if isinstance(text, str) and text.strip():
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": _as_object(function.get("arguments")),
                    }
                )
            if blocks:
                _append(out, "assistant", blocks)
            continue

        text = message.get("content")
        if isinstance(text, str) and text.strip():
            _append(out, "user", [{"type": "text", "text": text}])

    return "\n\n".join(system_parts), out


def _append(out: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    if out and out[-1]["role"] == role:
        out[-1]["content"].extend(blocks)
        return
    out.append({"role": role, "content": blocks})


def _as_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _as_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, default=str)


def _input_schema(parameters: dict[str, Any] | None) -> dict[str, Any]:
    """Anthropic requires an object schema. A module that declares nothing gets
    an empty object rather than a rejected request."""
    if isinstance(parameters, dict) and parameters.get("type") == "object":
        return parameters
    return {"type": "object", "properties": {}}


def _error_text(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:300]
    return str(body)[:300]
