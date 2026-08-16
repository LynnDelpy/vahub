"""Session trimming, which has to keep the history valid for the providers."""

from __future__ import annotations

import pytest

from vahub.agent.session import Session, SessionStore

# One full turn: a user message, an assistant reply that calls a tool, and the
# tool result. Repeating it builds a realistic long conversation.
_TURN = [
    {"role": "user", "content": "u"},
    {"role": "assistant", "content": "a", "tool_calls": [{"id": "c"}]},
    {"role": "tool", "content": "t", "tool_call_id": "c"},
]


@pytest.mark.parametrize("turns", range(1, 6))
def test_trim_keeps_system_and_never_starts_mid_turn(turns: int) -> None:
    store = SessionStore(max_messages=4)
    session = Session(id="x")
    session.messages = [{"role": "system", "content": "sys"}] + _TURN * turns
    store.trim(session)

    assert session.messages[0]["role"] == "system", "the security preamble must survive"
    kept = session.messages[1:]
    if kept:
        # A turn begins with a user message; starting on a tool result or an
        # assistant reply is a 400 from the providers.
        assert kept[0]["role"] == "user", kept
    assert len(session.messages) <= 4


def test_trim_leaves_short_history_untouched() -> None:
    store = SessionStore(max_messages=40)
    session = Session(id="x")
    session.messages = [{"role": "system", "content": "sys"}, *_TURN]
    store.trim(session)
    assert len(session.messages) == 4
