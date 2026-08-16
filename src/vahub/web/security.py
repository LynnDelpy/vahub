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


def origin_allowed(origin: str | None, allowlist: Iterable[str]) -> bool:
    """Whether a browser Origin may call the hub.

    The allowlist is the whole rule. An earlier version also trusted any Origin
    equal to the request's own Host header, to save the operator from listing
    their address; that is a DNS-rebinding hole, because a page the victim visits
    can rebind its own hostname to the hub's address and then present a matching
    Origin and Host. So the Origin must be in the configured allowlist and
    nowhere else. The loopback default and the proxy deployment both already list
    the address the browser actually uses.
    """
    if origin is None:
        # No Origin header is not a cross-site browser call: fetch and form
        # POSTs both send one. It is a non-browser client (curl, a script, a
        # health probe), which this rule was never meant to stop and which has
        # no ambient browser credentials to abuse.
        return True
    allow = {_normalise(a) for a in allowlist}
    if "*" in allow:
        return True
    return _normalise(origin) in allow


def check_origin(request: Request, config: Config) -> None:
    """Raise 403 when a browser presents an Origin that is not permitted."""
    if not origin_allowed(request.headers.get("origin"), config.web.origin_allowlist):
        raise HTTPException(status_code=403, detail="origin not allowed")


def websocket_origin_allowed(websocket: WebSocket, config: Config) -> bool:
    """Same rule as the REST routes. A WebSocket handshake is a plain GET that
    no browser policy restricts, so this is the only check standing in the way
    of a cross-site page reading the event stream."""
    return origin_allowed(websocket.headers.get("origin"), config.web.origin_allowlist)


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
