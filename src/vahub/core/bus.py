"""In-process publish/subscribe with a decided backpressure policy.

The publisher never blocks. Backpressure that is not decided decides itself,
wrongly: an unbounded queue is a memory leak the moment one dashboard socket
lags while a module floods `module.log` with stack traces.

Two policies, chosen per topic:

* DROP_OLDEST     bounded queue; on overflow the oldest message is dropped and
                  counted. Correct for streams where only the tail matters.
* DISCONNECT_SLOW bounded queue; on overflow the *subscriber* is dropped, not
                  the message. Correct for state transitions, where a consumer
                  that silently missed one would show a wrong world forever. The
                  UI reconnects and reloads its state over REST.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from . import metrics


class Backpressure(StrEnum):
    DROP_OLDEST = "drop_oldest"
    DISCONNECT_SLOW = "disconnect_slow"


# Convenient aliases so callers can write DROP_OLDEST instead of the enum path.
DROP_OLDEST = Backpressure.DROP_OLDEST
DISCONNECT_SLOW = Backpressure.DISCONNECT_SLOW


# Topics the hub publishes. Anything not listed gets the default policy, so an
# unknown topic degrades to "lossy but bounded" rather than to "unbounded".
TOPIC_POLICY: dict[str, tuple[Backpressure, int]] = {
    "module.state_changed": (Backpressure.DISCONNECT_SLOW, 256),
    "module.log": (Backpressure.DROP_OLDEST, 512),
    "tool.called": (Backpressure.DISCONNECT_SLOW, 256),
    "policy.confirmation_required": (Backpressure.DISCONNECT_SLOW, 64),
    "conversation.message": (Backpressure.DROP_OLDEST, 256),
    "schedule.fired": (Backpressure.DISCONNECT_SLOW, 64),
    "budget.exceeded": (Backpressure.DISCONNECT_SLOW, 64),
}
DEFAULT_POLICY: tuple[Backpressure, int] = (Backpressure.DROP_OLDEST, 128)

_SENTINEL = object()


@dataclass(eq=False)  # identity based: subscriptions live in a set, keyed by object
class Subscription:
    topic: str
    policy: Backpressure
    queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    _closed: bool = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Wake a consumer parked on get(); if the queue is full it will drain
        # into the loop's `closed` check anyway.
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(_SENTINEL)

    async def events(self) -> AsyncIterator[Any]:
        while not self._closed:
            item = await self.queue.get()
            if item is _SENTINEL:
                break
            yield item


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[Subscription]] = {}

    def subscribe(self, topic: str) -> Subscription:
        policy, maxsize = TOPIC_POLICY.get(topic, DEFAULT_POLICY)
        sub = Subscription(topic=topic, policy=policy, queue=asyncio.Queue(maxsize=maxsize))
        self._subs.setdefault(topic, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.topic)
        if subs is not None:
            subs.discard(sub)
        sub.close()

    def publish(self, topic: str, payload: Any) -> None:
        """Deliver to every subscriber of `topic`. Synchronous and non-blocking:
        it applies the topic's backpressure policy instead of waiting."""
        subs = self._subs.get(topic)
        if not subs:
            return
        for sub in list(subs):
            if sub.closed:
                subs.discard(sub)
                continue
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._overflow(sub, topic, payload)

    def _overflow(self, sub: Subscription, topic: str, payload: Any) -> None:
        metrics.BUS_DROPPED.labels(topic=topic).inc()
        if sub.policy is Backpressure.DROP_OLDEST:
            try:
                sub.queue.get_nowait()
                sub.queue.put_nowait(payload)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                # Another consumer raced us; dropping this message is the policy.
                pass
        else:
            self.unsubscribe(sub)

    def subscriber_count(self, topic: str | None = None) -> int:
        if topic is not None:
            return len(self._subs.get(topic, ()))
        return sum(len(s) for s in self._subs.values())

    def close(self) -> None:
        """Drop every subscriber. Used at shutdown so consumers stop cleanly
        instead of waiting on a queue nobody will ever fill again."""
        for subs in self._subs.values():
            for sub in list(subs):
                sub.close()
        self._subs.clear()
