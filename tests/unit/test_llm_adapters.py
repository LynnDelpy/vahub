"""The LLM adapters, with the HTTP call mocked.

The parsing is where the risk is: a tool-call argument string is model output,
not a contract, so it can be truncated, empty, or a JSON scalar, and none of
that may raise inside the agent loop. These tests feed the adapters canned
provider responses through an httpx MockTransport.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from vahub.agent.llm.anthropic import AnthropicLLM, _convert_messages
from vahub.agent.llm.base import LLMError, ToolSpec
from vahub.agent.llm.openai_compat import (
    OpenAICompatLLM,
    _parse_arguments,
    _parse_tool_calls,
)


def _mock(adapter: Any, responder) -> list[httpx.Request]:
    """Replace the adapter's client with one that answers from `responder`, and
    return a list the caller can read the captured requests from."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://mock"
    )
    return seen


# --------------------------------------------------------------------------
# openai_compat: pure parsing
# --------------------------------------------------------------------------
def test_parse_arguments_is_defensive() -> None:
    assert _parse_arguments({"a": 1}) == {"a": 1}
    assert _parse_arguments('{"a": 1}') == {"a": 1}
    assert _parse_arguments("") == {}
    assert _parse_arguments("   ") == {}
    assert _parse_arguments("not json") == {}
    assert _parse_arguments("[1, 2]") == {}   # a JSON array is not arguments
    assert _parse_arguments("42") == {}       # a scalar is not arguments
    assert _parse_arguments(None) == {}


def test_parse_tool_calls_skips_malformed_entries() -> None:
    raw = [
        {"id": "a", "function": {"name": "do", "arguments": '{"x": 1}'}},
        "not a dict",
        {"function": {"arguments": "{}"}},          # no name
        {"function": {"name": "", "arguments": "{}"}},  # empty name
        {"function": {"name": "later", "arguments": "bad"}},  # id falls back
    ]
    calls = _parse_tool_calls(raw)
    assert [c.name for c in calls] == ["do", "later"]
    assert calls[0].arguments == {"x": 1}
    assert calls[1].arguments == {}         # unparseable args -> empty
    assert calls[1].id == "call_4"          # id derived from index


def test_parse_tool_calls_of_a_non_list() -> None:
    assert _parse_tool_calls(None) == []
    assert _parse_tool_calls({}) == []


# --------------------------------------------------------------------------
# openai_compat: complete()
# --------------------------------------------------------------------------
def _openai() -> OpenAICompatLLM:
    return OpenAICompatLLM(base_url="http://mock", api_key="k", model="m")


async def test_openai_text_reply() -> None:
    adapter = _openai()
    _mock(adapter, lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 12},
    }))
    result = await adapter.complete([{"role": "user", "content": "hi"}], [])
    assert result.text == "hello" and not result.tool_calls
    assert result.stop_reason == "stop"
    assert result.usage and result.usage["total_tokens"] == 12


async def test_openai_tool_call_reply_and_tools_in_payload() -> None:
    adapter = _openai()
    seen = _mock(adapter, lambda r: httpx.Response(200, json={
        "choices": [{"message": {"tool_calls": [
            {"id": "c1", "function": {"name": "time__now", "arguments": '{"tz": "UTC"}'}},
        ]}}],
    }))
    specs = [ToolSpec(name="time__now", module="time", tool="now", description="d", parameters={})]
    result = await adapter.complete([{"role": "user", "content": "time?"}], specs)
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "time__now" and result.tool_calls[0].arguments == {"tz": "UTC"}
    # The request carried the tool catalogue.
    import json as _json

    body = _json.loads(seen[0].content)
    assert body["tools"][0]["function"]["name"] == "time__now"
    assert body["tool_choice"] == "auto"


def test_openai_sets_the_authorization_header() -> None:
    # Checked on the real client the adapter builds, before it is mocked.
    adapter = OpenAICompatLLM(base_url="http://mock", api_key="secret", model="m")
    assert adapter._client.headers["authorization"] == "Bearer secret"


async def test_openai_error_status_raises() -> None:
    adapter = _openai()
    _mock(adapter, lambda r: httpx.Response(500, json={"error": {"message": "boom"}}))
    with pytest.raises(LLMError, match="500"):
        await adapter.complete([{"role": "user", "content": "hi"}], [])


async def test_openai_non_json_body_raises() -> None:
    adapter = _openai()
    _mock(adapter, lambda r: httpx.Response(200, text="not json"))
    with pytest.raises(LLMError):
        await adapter.complete([{"role": "user", "content": "hi"}], [])


async def test_openai_empty_choices_is_an_empty_result() -> None:
    adapter = _openai()
    _mock(adapter, lambda r: httpx.Response(200, json={"choices": []}))
    result = await adapter.complete([{"role": "user", "content": "hi"}], [])
    assert result.text is None and result.tool_calls == []


# --------------------------------------------------------------------------
# anthropic
# --------------------------------------------------------------------------
def test_anthropic_message_conversion() -> None:
    system, msgs = _convert_messages([
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": '{"a": 1}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
    ])
    assert "be brief" in system
    # assistant with empty text but a tool_use block is kept (empty content is a
    # 400 from the API, so the block must carry it).
    assert any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_use" for b in m["content"])
        for m in msgs
    )
    assert any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
        for m in msgs
    )


def _anthropic() -> AnthropicLLM:
    return AnthropicLLM(base_url="http://mock", api_key="k", model="claude")


async def test_anthropic_parses_text_and_tool_use() -> None:
    adapter = _anthropic()
    _mock(adapter, lambda r: httpx.Response(200, json={
        "content": [
            {"type": "text", "text": "sure"},
            {"type": "tool_use", "id": "u1", "name": "time__now", "input": {"tz": "UTC"}},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 3},
        "stop_reason": "tool_use",
    }))
    result = await adapter.complete([{"role": "user", "content": "time?"}], [])
    assert result.text == "sure"
    assert result.tool_calls[0].name == "time__now" and result.tool_calls[0].arguments == {"tz": "UTC"}


async def test_anthropic_error_status_raises() -> None:
    adapter = _anthropic()
    _mock(adapter, lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}))
    with pytest.raises(LLMError, match="429"):
        await adapter.complete([{"role": "user", "content": "hi"}], [])
