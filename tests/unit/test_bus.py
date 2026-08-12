"""The event bus.

The property being defended is that publishing is never something the caller
has to think about. The supervisor publishes a state change while holding no
lock it wants to hold for long, and a browser tab that stopped reading its
WebSocket must not be able to slow that down or grow the process without bound.
So: publish is synchronous, it never blocks, and overflow is resolved by a
policy chosen per topic rather than by whoever fills the queue first.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from vahub.core import bus as bus_module
from vahub.core.bus import DISCONNECT_SLOW, DROP_OLDEST, EventBus

# Topics whose declared policy these tests rely on. Asserted before use, so a
# change of policy shows up as a clear failure here rather than as a confusing
# one three assertions later.
DROP_TOPIC = "module.log"
DISCONNECT_TOPIC = "module.state_changed"


async def drain(sub, limit: int = 10_000) -> list:
    out = []
    while not sub.queue.empty() and len(out) < limit:
        out.append(sub.queue.get_nowait())
    return out


def test_topic_policies_are_the_ones_these_tests_assume() -> None:
    bus = EventBus()
    assert bus.subscribe(DROP_TOPIC).policy == DROP_OLDEST
    assert bus.subscribe(DISCONNECT_TOPIC).policy == DISCONNECT_SLOW


def test_publish_is_not_a_coroutine() -> None:
    # An async publish is a publish that can be awaited, which is a publish that
    # can block. The signature is part of the guarantee.
    assert not inspect.iscoroutinefunction(EventBus.publish)


async def test_a_subscriber_receives_what_is_published(bus: EventBus) -> None:
    sub = bus.subscribe("module.log")
    bus.publish("module.log", {"module": "fake", "line": "hello"})
    assert await asyncio.wait_for(sub.queue.get(), 1) == {"module": "fake", "line": "hello"}


async def test_every_subscriber_of_a_topic_receives_the_event(bus: EventBus) -> None:
    subs = [bus.subscribe("module.log") for _ in range(3)]
    bus.publish("module.log", "one")
    assert [s.queue.get_nowait() for s in subs] == ["one", "one", "one"]


def test_publishing_to_a_topic_nobody_wants_is_harmless(bus: EventBus) -> None:
    bus.publish("module.log", "into the void")
    assert bus.subscriber_count("module.log") == 0


def test_subscribers_of_other_topics_do_not_see_it(bus: EventBus) -> None:
    logs = bus.subscribe("module.log")
    bus.publish("tool.called", {"tool": "echo"})
    assert logs.queue.empty()


async def test_events_iterator_yields_until_closed(bus: EventBus) -> None:
    sub = bus.subscribe("module.log")
    bus.publish("module.log", 1)
    bus.publish("module.log", 2)

    seen = []

    async def consume() -> None:
        async for event in sub.events():
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    bus.unsubscribe(sub)
    await asyncio.wait_for(task, 1)
    assert seen == [1, 2]


# --------------------------------------------------------------------------
# backpressure
# --------------------------------------------------------------------------
async def test_publisher_never_blocks_on_a_subscriber_that_never_reads(bus: EventBus) -> None:
    sub = bus.subscribe(DROP_TOPIC)
    overflow = sub.queue.maxsize * 4

    started = time.monotonic()
    for i in range(overflow):
        bus.publish(DROP_TOPIC, i)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "publishing past a full queue should not wait"
    assert sub.queue.qsize() <= sub.queue.maxsize, "the queue is bounded, so memory is bounded"


async def test_drop_oldest_keeps_the_newest_events(bus: EventBus) -> None:
    sub = bus.subscribe(DROP_TOPIC)
    total = sub.queue.maxsize + 10
    for i in range(total):
        bus.publish(DROP_TOPIC, i)

    kept = await drain(sub)
    assert kept[-1] == total - 1, "the most recent log line is the one worth keeping"
    assert kept[0] == total - len(kept)
    assert bus.subscriber_count(DROP_TOPIC) == 1, "a slow log reader is not disconnected"


async def test_disconnect_slow_drops_the_subscriber_not_the_event(bus: EventBus) -> None:
    # For state changes a gap is worse than a reconnect: a client that missed
    # `degraded` would keep showing `ready` forever.
    sub = bus.subscribe(DISCONNECT_TOPIC)
    for i in range(sub.queue.maxsize + 5):
        bus.publish(DISCONNECT_TOPIC, i)

    assert bus.subscriber_count(DISCONNECT_TOPIC) == 0
    assert sub.closed is True


async def test_a_disconnected_subscribers_iterator_finishes(bus: EventBus) -> None:
    sub = bus.subscribe(DISCONNECT_TOPIC)
    for i in range(sub.queue.maxsize + 5):
        bus.publish(DISCONNECT_TOPIC, i)

    async def consume() -> None:
        async for _ in sub.events():
            pass

    # It must end rather than park forever, or the WebSocket task leaks.
    await asyncio.wait_for(consume(), 2)


async def test_one_slow_subscriber_does_not_stall_the_others(bus: EventBus) -> None:
    stalled = bus.subscribe(DROP_TOPIC)
    attentive = bus.subscribe(DROP_TOPIC)

    received: list = []
    done = asyncio.Event()
    total = stalled.queue.maxsize * 2

    async def consume() -> None:
        async for event in attentive.events():
            received.append(event)
            if len(received) == total:
                done.set()
                return

    task = asyncio.create_task(consume())
    for i in range(total):
        bus.publish(DROP_TOPIC, i)
        if i % 32 == 0:
            await asyncio.sleep(0)  # let the attentive consumer run

    await asyncio.wait_for(done.wait(), 2)
    assert received == list(range(total))
    assert stalled.queue.qsize() == stalled.queue.maxsize  # it lost events, nobody else did
    task.cancel()


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
def test_unsubscribe_stops_delivery(bus: EventBus) -> None:
    sub = bus.subscribe("module.log")
    bus.unsubscribe(sub)
    bus.publish("module.log", "after")
    assert bus.subscriber_count("module.log") == 0


def test_unsubscribing_twice_is_not_an_error(bus: EventBus) -> None:
    sub = bus.subscribe("module.log")
    bus.unsubscribe(sub)
    bus.unsubscribe(sub)


def test_a_closed_subscription_is_forgotten_on_the_next_publish(bus: EventBus) -> None:
    sub = bus.subscribe("module.log")
    sub.close()
    bus.publish("module.log", "anything")
    assert bus.subscriber_count("module.log") == 0


def test_an_unknown_topic_gets_a_bounded_default(bus: EventBus) -> None:
    sub = bus.subscribe("something.nobody.declared")
    assert sub.queue.maxsize > 0
    assert sub.policy in (DROP_OLDEST, DISCONNECT_SLOW)


def test_two_subscriptions_are_distinct_even_on_one_topic(bus: EventBus) -> None:
    first = bus.subscribe("module.log")
    second = bus.subscribe("module.log")
    assert first is not second
    bus.unsubscribe(first)
    assert bus.subscriber_count("module.log") == 1


@pytest.mark.parametrize("topic", ["module.state_changed", "policy.confirmation_required"])
def test_topics_the_ui_depends_on_are_declared(topic: str) -> None:
    # A topic that falls through to the default gets the default's policy, which
    # for these would silently drop a confirmation prompt.
    assert topic in bus_module.TOPIC_POLICY
