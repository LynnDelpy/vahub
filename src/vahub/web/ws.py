"""The live event stream the assistant page listens on.

It carries one thing: the `policy.confirmation_required` event, so a held-back
destructive action appears without the page having to poll. Everything else on
the bus (module state, module stderr) is operator information and stays on the
host.

Three decisions that are not obvious from the code:

* One task does every send. Starlette's send is not safe to call concurrently;
  two tasks writing frames interleave and corrupt the stream. Every subscription
  is therefore merged into a single queue with one consumer.
* Subscriptions are released on every exit path, including a failure of the very
  first send. A leaked subscription is a queue the bus keeps filling for the
  lifetime of the process.
* When the bus drops a subscriber (its `disconnect_slow` policy), the socket is
  closed rather than continued. A page that silently missed a confirmation event
  is worse than one that reconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import auth
from .security import websocket_origin_allowed

if TYPE_CHECKING:
    from ..core.runtime import Runtime

# Bus topic -> the short kind the assistant page switches on.
# Only what the person talking to the assistant needs. Module states and module
# stderr are operator concerns: they belong in the service log and in the CLI,
# not in a page that may be handed to someone who just wants to ask a question.
TOPICS: tuple[tuple[str, str], ...] = (("policy.confirmation_required", "confirm"),)

MERGED_MAXSIZE = 1024


async def _feed(sub: Any, kind: str, merged: asyncio.Queue) -> None:
    """Move one subscription's events into the merged queue.

    The put is awaited on purpose: when the browser cannot keep up, the pressure
    travels back to the bus subscription, where the topic's own policy (drop the
    oldest, or drop this subscriber) decides what happens. It never reaches the
    publisher, which must not block.
    """
    async for event in sub.events():
        await merged.put((kind, event))


async def _drain_client(websocket: WebSocket) -> None:
    """Consume and discard client frames.

    The socket is server-to-client, but the disconnect only becomes visible by
    receiving, so this is how a closed tab is noticed promptly instead of on the
    next event, which may be minutes away.
    """
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


async def _send_loop(websocket: WebSocket, merged: asyncio.Queue) -> None:
    while True:
        kind, event = await merged.get()
        await websocket.send_json({"type": kind, "data": event})


def build_router(rt: Runtime) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/events")
    async def events(websocket: WebSocket) -> None:
        # A WebSocket handshake is a GET that no browser policy restricts, so
        # this check is the only thing stopping a cross-site page from reading
        # the hub's event stream.
        if not websocket_origin_allowed(websocket, rt.config):
            await websocket.close(code=1008)
            return
        # The event stream carries pending confirmations, so it is gated by the
        # same login as the API when auth is on.
        if rt.config.web.auth.enabled and await auth.username_from_cookies(websocket.cookies, rt) is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()

        subs: list[tuple[Any, str]] = []
        tasks: list[asyncio.Task] = []
        try:
            subs = [(rt.bus.subscribe(topic), kind) for topic, kind in TOPICS]
            merged: asyncio.Queue = asyncio.Queue(maxsize=MERGED_MAXSIZE)
            await websocket.send_json({"type": "ready"})
            tasks = [asyncio.create_task(_feed(sub, kind, merged)) for sub, kind in subs]
            tasks.append(asyncio.create_task(_send_loop(websocket, merged)))
            tasks.append(asyncio.create_task(_drain_client(websocket)))
            done, _running = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                with contextlib.suppress(asyncio.CancelledError):
                    task.exception()  # retrieved so a failed task does not warn at GC
        except (WebSocketDisconnect, RuntimeError):
            # RuntimeError is what Starlette raises when the socket is already
            # gone; neither case is worth a stack trace in the log.
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for sub, _ in subs:
                rt.bus.unsubscribe(sub)
            with contextlib.suppress(RuntimeError):
                await websocket.close()

    return router


__all__ = ["build_router"]
