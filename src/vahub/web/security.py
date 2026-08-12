"""Origin checks for state-changing requests, and the proxy's auth subject.

The hub authenticates nobody: that is the reverse proxy's job. What this module
defends against is different, and it applies even on a loopback-only install: a
page on some other site can make the operator's browser POST to the hub, or open
a WebSocket to it. The same-origin policy does not stop the request from being
*sent*, and it never applied to WebSockets at all. So every state-changing route
checks Origin explicitly.

Two decisions worth stating:

* A missing Origin is allowed. Browsers attach Origin to every cross-origin
  request and to same-origin POSTs; its absence means a non-browser client
  (curl, a script on the box), which cannot be tricked into riding an operator's
  session because there is no session to ride.
* An Origin that matches the request's own Host is allowed regardless of the
  allowlist. A hostile page cannot forge Origin, so an Origin naming this very
  server came from a page this server served. This removes the usual footgun
  where the console is opened on http://127.0.0.1:8080 while the allowlist says
  http://localhost:8080 and every POST fails with 403.

The subject header is informational. It is recorded in the audit log as the
acting principal and is never an authorization input, because anything that can
reach the hub directly can set it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from fastapi import Request, WebSocket

    from ..config.models import Config

# A subject is a label for a log line, not a credential. Bound it so a hostile
# proxy header cannot write megabytes into the audit table.
MAX_SUBJECT_LEN = 128


def _normalise(origin: str) -> str:
    return origin.strip().rstrip("/").casefold()


def _self_origins(host_header: str | None) -> set[str]:
    """The origins that name this server. Both schemes are accepted because a
    TLS-terminating proxy leaves the hub seeing plain http while the browser
    reports https."""
    if not host_header:
        return set()
    host = _normalise(host_header)
    return {f"http://{host}", f"https://{host}"}


def origin_allowed(origin: str | None, allowlist: Iterable[str], host_header: str | None = None) -> bool:
    if origin is None:
        return True
    allow = {_normalise(a) for a in allowlist}
    if "*" in allow:
        return True
    candidate = _normalise(origin)
    return candidate in allow or candidate in _self_origins(host_header)


def check_origin(request: Request, config: Config) -> None:
    """Raise 403 when a browser presents an Origin that is not permitted."""
    if not origin_allowed(
        request.headers.get("origin"),
        config.web.origin_allowlist,
        request.headers.get("host"),
    ):
        raise HTTPException(status_code=403, detail="origin not allowed")


def websocket_origin_allowed(websocket: WebSocket, config: Config) -> bool:
    """Same rule as the REST routes. A WebSocket handshake is a plain GET that
    no browser policy restricts, so this is the only check standing in the way
    of a cross-site page reading the event stream."""
    return origin_allowed(
        websocket.headers.get("origin"),
        config.web.origin_allowlist,
        websocket.headers.get("host"),
    )


def auth_subject(request: Request, config: Config) -> str | None:
    """Read the subject the authenticating proxy claims, for the audit log."""
    raw = request.headers.get(config.web.auth_subject_header)
    if not raw:
        return None
    # Control characters would corrupt a log line; the header is attacker
    # controlled whenever the proxy is bypassed.
    cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()
    return cleaned[:MAX_SUBJECT_LEN] or None


__all__ = [
    "MAX_SUBJECT_LEN",
    "auth_subject",
    "check_origin",
    "origin_allowed",
    "websocket_origin_allowed",
]
