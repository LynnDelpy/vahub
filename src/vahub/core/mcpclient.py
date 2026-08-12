"""A minimal MCP client speaking JSON-RPC 2.0 over stdio.

The Supervisor owns the subprocess (spawn, uid, restart, health). This class
owns only the wire protocol and the request/response id map. Keeping the two
apart is the point of the module contract: replacing stdio with another
transport later should not touch lifecycle code.

Two decisions worth stating.

A timeout is not a cancellation. When a caller gives up, its waiter is removed
from the id map, so a reply that arrives afterwards matches nothing and is
discarded. Handing a late reply to the next caller is how "turn on the hallway
light" ends up answering with the state of the living room.

We announce no client capabilities: no `roots`, no `sampling`. A module
therefore cannot ask the hub to run an inference on its behalf, and any
server to client request is refused here rather than being quietly ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

# A protocol revision current MCP servers accept. We send this and, for the
# tools-only surface used here, tolerate a server echoing a different one.
PROTOCOL_VERSION = "2025-06-18"

CLIENT_NAME = "vahub"

# Anything longer than this on one line is treated as a broken module rather
# than buffered. MCP stdio framing is one JSON message per line.
LINE_LIMIT = 1024 * 1024

RequestId = int | str


class McpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


def _key(raw: Any) -> RequestId | None:
    """Normalise a JSON-RPC id into a dict key.

    A bool is not an id (and `True == 1` in Python, which would alias request 1).
    A numeric string is accepted as the number it spells: a server that echoes
    the id with the wrong JSON type is sloppy, not hostile, and treating its
    reply as an orphan would only produce a spurious timeout."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return raw
    return None


class McpClient:
    def __init__(
        self,
        name: str,
        stdin: asyncio.StreamWriter,
        stdout: asyncio.StreamReader,
        log: Any,
    ) -> None:
        self._name = name
        self._stdin = stdin
        self._stdout = stdout
        self._log = log
        self._next_id = 0
        self._pending: dict[RequestId, asyncio.Future[Any]] = {}
        self._read_task: asyncio.Task[None] | None = None
        self._closed = False
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}

    def start(self) -> None:
        self._read_task = asyncio.create_task(self._read_loop(), name=f"mcp-read:{self._name}")

    # --- read side ---------------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    line = await self._stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as e:
                    # An oversized line leaves the stream buffer in a state we
                    # cannot resynchronise. Treat it as a broken connection
                    # instead of crashing the task or looping on the same data.
                    self._log.warning("mcp_line_too_long", error=str(e))
                    break
                if not line:
                    break  # EOF: the process is gone
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    msg = json.loads(stripped)
                except ValueError:
                    self._log.warning("mcp_bad_json", raw=stripped[:200].decode("utf-8", "replace"))
                    continue
                if not isinstance(msg, dict):
                    self._log.warning("mcp_non_object_message")
                    continue
                try:
                    self._dispatch(msg)
                except Exception as e:  # one malformed message must not end the loop
                    self._log.warning("mcp_dispatch_error", error=str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            self._log.warning("mcp_read_loop_error", error=str(e))
        finally:
            self._fail_all(McpError(-1, "connection closed"))

    def _dispatch(self, msg: dict[str, Any]) -> None:
        has_id = "id" in msg
        key = _key(msg.get("id")) if has_id else None

        if has_id and ("result" in msg or "error" in msg):
            fut = self._pending.pop(key, None) if key is not None else None
            if fut is None:
                # The caller already timed out and dropped its waiter, or the id
                # was never ours. Discard: never hand this to another caller.
                self._log.debug("mcp_orphan_response", id=msg.get("id"))
                return
            if fut.done():
                return
            if "error" in msg:
                err = msg["error"] if isinstance(msg.get("error"), dict) else {}
                code = err.get("code", -1)
                fut.set_exception(
                    McpError(
                        code if isinstance(code, int) else -1,
                        str(err.get("message", "module returned an error")),
                        err.get("data"),
                    )
                )
            else:
                fut.set_result(msg.get("result"))
            return

        method = msg.get("method")
        if not isinstance(method, str):
            self._log.debug("mcp_unroutable_message")
            return

        if has_id:
            # A server to client request. We announced no capabilities, so this
            # is refused explicitly: silence would leave the module waiting.
            self._log.warning("mcp_server_request_refused", method=method)
            with contextlib.suppress(McpError):
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {"code": -32601, "message": f"capability not supported: {method}"},
                    }
                )
            return

        self._log.debug("mcp_notification", method=method)

    # --- write side --------------------------------------------------------
    def _send(self, msg: dict[str, Any]) -> None:
        if self._closed or self._stdin.is_closing():
            raise McpError(-1, "transport closing")
        try:
            self._stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        except (OSError, RuntimeError) as e:
            raise McpError(-1, f"write failed: {e}") from e

    async def request(self, method: str, params: dict[str, Any] | None, timeout_s: float) -> Any:
        if self._closed:
            raise McpError(-1, "client closed")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        except Exception:
            self._pending.pop(rid, None)  # do not leak a waiter nobody will resolve
            raise
        try:
            return await asyncio.wait_for(fut, timeout_s)
        except (TimeoutError, asyncio.CancelledError):
            # Drop the waiter here, so a late reply carrying this id finds
            # nothing in the map and is discarded by _dispatch.
            self._pending.pop(rid, None)
            raise

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # --- high level --------------------------------------------------------
    async def initialize(self, timeout_s: float) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},  # announce nothing: no roots, no sampling
                "clientInfo": {"name": CLIENT_NAME, "version": _version()},
            },
            timeout_s,
        )
        if not isinstance(result, dict):
            raise McpError(-1, "initialize returned a non-object result")
        info = result.get("serverInfo")
        caps = result.get("capabilities")
        self.server_info = info if isinstance(info, dict) else {}
        self.server_capabilities = caps if isinstance(caps, dict) else {}
        if "tools" not in self.server_capabilities:
            raise McpError(-1, "module announces no tools capability")
        self.notify("notifications/initialized")
        return result

    async def list_tools(self, timeout_s: float) -> list[dict[str, Any]]:
        result = await self.request("tools/list", {}, timeout_s)
        if not isinstance(result, dict):
            raise McpError(-1, "tools/list returned a non-object result")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpError(-1, "tools/list returned no tool list")
        # Entries without a usable name are dropped rather than carried around
        # as holes every later caller has to check for.
        return [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: float) -> Any:
        return await self.request("tools/call", {"name": name, "arguments": arguments}, timeout_s)

    # --- teardown ----------------------------------------------------------
    def _fail_all(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def close(self) -> None:
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass  # expected: we cancelled it
            except Exception:  # pragma: no cover - defensive
                pass
            self._read_task = None
        try:
            if not self._stdin.is_closing():
                self._stdin.close()
        except (OSError, RuntimeError):  # pragma: no cover - transport already gone
            pass
        self._fail_all(McpError(-1, "closed"))


def _version() -> str:
    from ..__about__ import __version__

    return __version__
