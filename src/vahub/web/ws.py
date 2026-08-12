"""The live event stream the console listens on.

Three decisions that are not obvious from the code:

* One task does every send. Starlette's send is not safe to call concurrently;
  two tasks writing frames interleave and corrupt the stream. Every subscription
  is therefore merged into a single queue with one consumer.
* Subscriptions are released on every exit path, including a failure of the very
  first snapshot send. A leaked subscription is a queue the bus keeps filling for
  the lifetime of the process.
* When the bus drops a subscriber (its `disconnect_slow` policy), the socket is
  closed rather than continued. A console that silently missed events is worse
  than one that reconnects and reloads its state over REST.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .api import module_view
from .security import websocket_origin_allowed

if TYPE_CHECKING:
    from ..core.runtime import Runtime

# Bus topic -> the short kind the console switches on.
TOPICS: tuple[tuple[str, str], ...] = (
    ("module.state_changed", "state"),
    ("module.log", "log"),
    ("policy.confirmation_required", "confirm"),
    ("schedule.fired", "schedule"),
    ("budget.exceeded", "budget"),
)

MERGED_MAXSIZE = 1024
# A module controls its own stderr. Truncating here keeps one pathological line
# from being pushed at every open console.
MAX_LOG_LINE_CHARS = 2_000


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


def _shrink(kind: str, event: Any) -> Any:
    if kind != "log" or not isinstance(event, dict):
        return event
    line = event.get("line")
    if isinstance(line, str) and len(line) > MAX_LOG_LINE_CHARS:
        return {**event, "line": line[:MAX_LOG_LINE_CHARS] + " ...[truncated]"}
    return event


async def _send_loop(websocket: WebSocket, merged: asyncio.Queue) -> None:
    while True:
        kind, event = await merged.get()
        await websocket.send_json({"type": kind, "data": _shrink(kind, event)})


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
        await websocket.accept()

        subs: list[tuple[Any, str]] = []
        tasks: list[asyncio.Task] = []
        try:
            subs = [(rt.bus.subscribe(topic), kind) for topic, kind in TOPICS]
            merged: asyncio.Queue = asyncio.Queue(maxsize=MERGED_MAXSIZE)
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "modules": [module_view(m) for m in rt.supervisor.modules.values()],
                }
            )
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
