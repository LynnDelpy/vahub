"""The ASGI application: routers, probes, and the console page.

Two decisions worth stating.

* The interactive API docs are switched off. Swagger UI loads its assets from a
  public CDN, which would both break an offline LAN install and make the console
  page talk to the internet; neither is acceptable for a hub that controls locks
  and lights.
* The console is served with a per-response CSP nonce rather than
  `script-src 'unsafe-inline'`. The page is a single self-contained file with an
  inline script, and a nonce keeps the "no injected script can run" property that
  the rest of the console's rendering rules are built on.

`/health` answers as long as the process is up. `/ready` is stricter: it reports
not-ready while any module is still in its first handshake, which is what a load
balancer or a systemd readiness check should wait for.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..__about__ import __version__
from ..core import metrics
from . import api, ws
from .api import state_value

if TYPE_CHECKING:
    from ..core.runtime import Runtime

STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"
NONCE_PLACEHOLDER = "__CSP_NONCE__"

# Applied to every response. The console overrides Content-Security-Policy with
# its own, looser, policy; everything else is JSON that needs nothing at all.
_BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
}


def _console_csp(nonce: str) -> str:
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        "img-src 'self' data:; "
        "media-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )


def create_app(rt: Runtime) -> FastAPI:
    app = FastAPI(
        title="vahub",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for header, value in _BASE_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        counts: dict[str, int] = {}
        for module in rt.supervisor.modules.values():
            state = state_value(module.state)
            counts[state] = counts.get(state, 0) + 1
        # A module that is still starting has not finished its handshake, so its
        # tools do not exist yet. Everything else, including failed, is settled:
        # the hub is up and reports the failure rather than hiding behind 503.
        settled = counts.get("starting", 0) == 0
        return JSONResponse(
            {"ready": settled, "modules": counts},
            status_code=200 if settled else 503,
        )

    @app.get("/metrics")
    async def prometheus() -> Response:
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    @app.get("/")
    async def index() -> HTMLResponse:
        nonce = secrets.token_urlsafe(16)
        html = INDEX_FILE.read_text(encoding="utf-8").replace(NONCE_PLACEHOLDER, nonce)
        return HTMLResponse(
            html,
            headers={
                "Content-Security-Policy": _console_csp(nonce),
                # The nonce must not be reused, so the page itself is never cached.
                "Cache-Control": "no-store",
            },
        )

    app.include_router(api.build_router(rt))
    app.include_router(ws.build_router(rt))
    return app


__all__ = ["create_app"]
