"""In-memory conversation sessions.

Sessions live for the process lifetime and reset on restart; the durable record
of what was said is the store, not this. Two things here are less obvious than
they look:

* Trimming keeps the system message. It is the message that tells the model tool
  results are data, so dropping it as "the oldest message" would quietly remove
  a security property halfway through a long conversation.
* Trimming never leaves history starting mid-turn. A tool result with no
  preceding assistant call, or an assistant reply with no preceding user
  message, is a 400 from the providers (Anthropic in particular requires the
  first message after the system prompt to be a user message). So the tail is
  advanced to the next user message, which is where a turn actually begins.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

Message = dict[str, Any]


@dataclass
class Session:
    id: str
    messages: list[Message] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()


class SessionStore:
    def __init__(self, max_messages: int = 40, max_sessions: int = 200) -> None:
        # A hub that is left running accumulates one session per browser tab and
        # per voice client, so the store evicts the least recently used rather
        # than growing for the lifetime of the process.
        self._sessions: dict[str, Session] = {}
        self._max_messages = max_messages
        self._max_sessions = max_sessions

    def __len__(self) -> int:
        return len(self._sessions)

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: str | None = None) -> Session:
        if session_id and (existing := self._sessions.get(session_id)) is not None:
            existing.touch()
            return existing
        sid = session_id or uuid.uuid4().hex
        session = Session(id=sid)
        self._sessions[sid] = session
        self._evict()
        return session

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def trim(self, session: Session) -> None:
        """Keep history bounded: the system message plus the most recent turns."""
        session.touch()
        messages = session.messages
        if len(messages) <= self._max_messages:
            return

        head: list[Message] = []
        body = messages
        if messages and messages[0].get("role") == "system":
            head, body = messages[:1], messages[1:]

        keep = max(self._max_messages - len(head), 1)
        start = max(len(body) - keep, 0)
        # A turn begins with a user message. Starting the kept history on a tool
        # result (no preceding assistant call) or an assistant reply (no
        # preceding user message) is rejected by the providers, so advance to the
        # next user message.
        while start < len(body) and body[start].get("role") in ("tool", "assistant"):
            start += 1
        session.messages = head + body[start:]

    def _evict(self) -> None:
        if len(self._sessions) <= self._max_sessions:
            return
        overflow = len(self._sessions) - self._max_sessions
        oldest = sorted(self._sessions.items(), key=lambda kv: kv[1].updated_at)[:overflow]
        for sid, _ in oldest:
            del self._sessions[sid]
