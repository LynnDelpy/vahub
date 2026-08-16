"""The web login: sessions, the session cookie, and the route guard.

When `web.auth.enabled` is on, every route except the login itself, the page
shell, the health probes and the static assets requires a valid session. A
session is an opaque random token stored in the database (so it can be revoked)
and carried in an HttpOnly, SameSite=Strict cookie. Combined with the existing
Origin check, that cookie is not usable by a cross-site page.

Accounts are created by the CLI, never here: this module verifies a password and
issues a session, it does not register anyone.
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict
from collections.abc import Mapping
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import hash_password, needs_rehash, verify_password
from .security import check_origin

if TYPE_CHECKING:
    from ..core.runtime import Runtime

SESSION_COOKIE = "vahub_session"

# Paths reachable without a session. Everything else is guarded when auth is on.
# The page shell and /api/me must load so the client can show a login form; the
# login endpoint obviously cannot require being logged in.
_PUBLIC_PATHS = frozenset({"/", "/health", "/ready", "/api/login", "/api/me"})
_PUBLIC_PREFIXES = ("/static/",)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class _Throttle:
    """A small in-process brake on password guessing. After a handful of failures
    for one username, further attempts are refused for a cool-off window. It is
    intentionally simple and per-process; a serious deployment authenticates at
    the proxy. Successful logins clear the count."""

    def __init__(self, limit: int = 5, window_s: float = 300.0) -> None:
        self._limit = limit
        self._window_s = window_s
        self._fails: dict[str, list[float]] = defaultdict(list)

    def locked(self, key: str, now: float) -> bool:
        recent = [t for t in self._fails.get(key, ()) if now - t < self._window_s]
        self._fails[key] = recent
        return len(recent) >= self._limit

    def record_failure(self, key: str, now: float) -> None:
        self._fails[key].append(now)

    def clear(self, key: str) -> None:
        self._fails.pop(key, None)


def is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


async def username_from_cookies(cookies: Mapping[str, str], rt: Runtime) -> str | None:
    """The signed-in username for a session cookie, or None. Works for both an
    HTTP request and a WebSocket, which each expose `.cookies`."""
    sid = cookies.get(SESSION_COOKIE)
    if not sid or rt.store is None:
        return None
    return await rt.store.session_user(sid)


async def current_username(request: Request, rt: Runtime) -> str | None:
    """The signed-in username for this request, or None."""
    return await username_from_cookies(request.cookies, rt)


def build_router(rt: Runtime) -> APIRouter:
    router = APIRouter()
    throttle = _Throttle()

    @router.get("/me")
    async def me(request: Request) -> JSONResponse:
        """What the page needs to decide between the assistant and a login form.
        Never requires auth; it is how the client learns whether it must."""
        auth = rt.config.web.auth
        if not auth.enabled:
            return JSONResponse({"auth": False})
        setup_required = rt.store is not None and await rt.store.count_users() == 0
        username = await current_username(request, rt)
        display = None
        if username and rt.store is not None:
            user = await rt.store.get_user(username)
            display = (user or {}).get("display_name") if user else None
        return JSONResponse(
            {
                "auth": True,
                "setup_required": setup_required,
                "authenticated": username is not None,
                "username": username,
                "display_name": display,
            }
        )

    @router.post("/login")
    async def login(body: LoginBody, request: Request) -> Response:
        check_origin(request, rt.config)
        auth = rt.config.web.auth
        if not auth.enabled or rt.store is None:
            return JSONResponse({"ok": False, "error": "auth_disabled"}, status_code=400)

        now = time.time()
        key = body.username.lower()
        if throttle.locked(key, now):
            return JSONResponse(
                {"ok": False, "error": "too_many_attempts"}, status_code=429
            )

        user = await rt.store.get_user(body.username)
        # Verify even when the user is missing, against a throwaway hash, so the
        # response time does not reveal whether the username exists.
        stored = (user or {}).get("password_hash") or _DUMMY_HASH
        ok = verify_password(body.password, stored)
        if user is None or user.get("disabled") or not ok:
            throttle.record_failure(key, now)
            return JSONResponse({"ok": False, "error": "invalid_credentials"}, status_code=401)

        throttle.clear(key)
        # Opportunistically upgrade an old hash now that the password is in hand.
        if needs_rehash(stored):
            await rt.store.set_password(body.username, hash_password(body.password))

        sid = secrets.token_urlsafe(32)
        await rt.store.create_session(sid, body.username, now + auth.session_ttl_s)
        response = JSONResponse({"ok": True, "username": body.username})
        response.set_cookie(
            SESSION_COOKIE,
            sid,
            max_age=int(auth.session_ttl_s),
            httponly=True,
            samesite="strict",
            secure=auth.cookie_secure,
            path="/",
        )
        return response

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        check_origin(request, rt.config)
        sid = request.cookies.get(SESSION_COOKIE)
        if sid and rt.store is not None:
            await rt.store.delete_session(sid)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router


# A fixed, valid scrypt hash used only to keep the failure path's timing similar
# to the success path. It matches no real password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
