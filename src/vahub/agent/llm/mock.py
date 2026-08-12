"""A deterministic, keyword-driven stub backend.

This is not natural language understanding and does not pretend to be. It exists
so the whole loop (message, choose a tool, call it through the gate, answer) can
be exercised in tests and demos with no credentials and no network. Because it
is deterministic, a test can assert the exact tool call a phrase produces.

Intents are yielded in priority order and the first one whose tool is actually in
the catalog wins, so a tool the policy hides is hidden from the stub too.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from .base import LLMResult, ToolCall, ToolSpec

_CITY_TZ = {
    "tokyo": "Asia/Tokyo",
    "berlin": "Europe/Berlin",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "new york": "America/New_York",
    "sydney": "Australia/Sydney",
    "utc": "UTC",
}
_ROOM_LIGHT = {
    "bedroom": "light.schlafzimmer",
    "schlafzimmer": "light.schlafzimmer",
    "living": "light.wohnzimmer",
    "wohnzimmer": "light.wohnzimmer",
    "hall": "light.flur",
    "flur": "light.flur",
    "kitchen": "light.kueche",
    "kueche": "light.kueche",
}
_PERCENT = re.compile(r"(\d{1,3})\s*%")
_OFF_WORDS = ("turn off", "switch off", " off")
_ON_WORDS = ("turn on", "switch on", " on", "dim", "brighten")
_TIME_WORDS = ("time", "clock", "hour", "uhr", "o'clock")
_LIST_WORDS = ("what devices", "list", "entities", "what's on")

_HELP = (
    "I am a keyword stub for testing the loop, not a language model. I can tell the time and control "
    "the demo home (try 'turn on the bedroom light', 'what is the temperature', 'unlock the front "
    "door'). Set llm.provider to openai_compat or anthropic for real understanding. "
)


class MockLLM:
    async def complete(self, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMResult:
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            return LLMResult(text=_answer_from_result(last.get("content") or "{}"))

        text = _last_user_text(messages).lower()
        by_tool = {spec.tool: spec for spec in tools}
        for tool, arguments in _intents(text):
            spec = by_tool.get(tool)
            if spec is not None:
                return LLMResult(tool_calls=[ToolCall(id="call_1", name=spec.name, arguments=arguments)])

        available = ", ".join(spec.name for spec in tools) or "none"
        return LLMResult(text=_HELP + f"Tools I can see: {available}.")

    async def aclose(self) -> None:
        return None


def _intents(text: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Candidate tool calls for one utterance, best guess first."""
    # "unlock" contains "lock", so the order of these two decides the meaning.
    if "unlock" in text:
        yield "lock_unlock", {"entity_id": "lock.haustuer"}
    elif "lock" in text and "door" in text:
        yield "lock_lock", {"entity_id": "lock.haustuer"}

    light = _match_light(text)
    if light is not None:
        if any(word in text for word in _OFF_WORDS):
            yield "light_turn_off", {"entity_id": light}
        if any(word in text for word in _ON_WORDS):
            arguments: dict[str, Any] = {"entity_id": light}
            percent = _match_percent(text)
            if percent is not None:
                arguments["brightness_pct"] = percent
            yield "light_turn_on", arguments

    if "temperature" in text or "temp" in text:
        yield "get_state", {"entity_id": "sensor.temperatur"}

    if any(word in text for word in _LIST_WORDS):
        yield "list_entities", {}

    if any(word in text for word in _TIME_WORDS):
        arguments = {}
        tz = _match_tz(text)
        if tz is not None:
            arguments["tz"] = tz
        if any(word in text for word in ("say", "tell", "speak", "sag")):
            yield "speak_current_time", arguments
        yield "get_current_time", arguments


def _answer_from_result(content: str) -> str:
    try:
        data = json.loads(content)
    except ValueError:
        return str(content)
    if not isinstance(data, dict):
        return str(data)
    if data.get("error") == "confirmation_required":
        return "That needs confirmation before I can do it."
    if data.get("ok") is False:
        return f"That did not work: {data.get('error')} ({data.get('detail')})."

    value = data.get("result")
    if isinstance(value, list):
        return f"I found {len(value)} entities."
    if isinstance(value, dict):
        if "state" in value:
            return f"{value.get('entity_id', 'It')} is {value['state']}."
        if "entity_id" in value:
            return f"Done ({value['entity_id']})."
        return json.dumps(value, default=str)
    return str(value)


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


def _match_tz(text: str) -> str | None:
    for city, tz in _CITY_TZ.items():
        if city in text:
            return tz
    return None


def _match_light(text: str) -> str | None:
    for room, entity in _ROOM_LIGHT.items():
        if room in text:
            return entity
    if "light" in text:  # an unqualified "the light" means the living room
        return "light.wohnzimmer"
    return None


def _match_percent(text: str) -> int | None:
    match = _PERCENT.search(text)
    if match is None:
        return None
    return max(1, min(100, int(match.group(1))))
