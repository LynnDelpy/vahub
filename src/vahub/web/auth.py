"""The web login: sessions, the session cookie, roles, and the route guard.

When `web.auth.enabled` is on, every route except the login itself, the page
shell, the health probes and the static assets requires a valid session. A
session is an opaque random token stored in the database (so it can be revoked)
and carried in an HttpOnly, SameSite=Strict cookie. Combined with the existing
Origin check, that cookie is not usable by a cross-site page.

An account holds a role, and this module is where a request is turned into one.
Two things about that are deliberate:

* The role is read from the account on every request (`session_identity` joins
  `users`), never copied into the session at login. Demoting or disabling
  somebody therefore takes effect on their next request, not when their cookie
  expires.
* With `web.auth.enabled` off there are no accounts at all, so there is nobody
  to be an admin and nobody to keep out: the hub is behind a proxy that decided
  who may reach it, and `is_admin` is true for everyone. Roles are a division of
  the built-in login, not a second authentication.

The first account created (by `vahub user add` or by the first visitor at
first run) is an admin; later web-created accounts default to `user`.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import (
    ROLE_ADMIN,
    ROLE_USER,
    hash_password,
    needs_rehash,
    password_error,
    username_error,
    verify_password,
)
from .security import check_origin

if TYPE_CHECKING:
    from ..core.runtime import Runtime

SESSION_COOKIE = "vahub_session"

# Sentinel for "this request has not been resolved yet", so that a genuine
# "nobody is signed in" (None) is memoised rather than resolved again.
_UNSET: object = object()


@dataclass(frozen=True, slots=True)
class Identity:
    """Who is making this request, and what they may do."""

    username: str
    role: str
    display_name: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


# Paths reachable without a session. Everything else is guarded when auth is on.
# The page shell and /api/me must load so the client can show a login form; the
# login endpoint obviously cannot require being logged in; /metrics is scraped by
# a machine that has no session (it is still origin-checked, and the proxy 404s
# it for clients).
_PUBLIC_PATHS = frozenset({"/", "/health", "/ready", "/metrics", "/api/login", "/api/me", "/api/setup"})
_PUBLIC_PREFIXES = ("/static/",)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class SetupBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str | None = Field(default=None, max_length=80)


class Throttle:
    """A small in-process brake on password guessing. After a handful of failures
    for one username, further attempts are refused for a cool-off window. It is
    intentionally simple and per-process; a serious deployment authenticates at
    the proxy. Successful logins clear the count.

    It brakes two things, but only as far as its key reaches. The obvious one is
    guessing. The other is cost: verifying a password is a deliberately expensive
    32 MiB scrypt, so any route that verifies one is also a way to spend the
    machine's memory and CPU, and every such route uses one of these.

    The limit is worth stating plainly: the key is a name, not a caller. Where
    the caller chooses that name (the login, where it is the submitted username)
    an attacker rotating names is never braked at all, and one who guesses a real
    name can lock its owner out of the login form for the window. Where the name
    is the *authenticated* account (changing your own password) neither applies,
    because a caller has exactly one. Fixing the login case needs a second bucket
    keyed on the client address, which is a proxy question as much as a code
    one; a deployment that cares authenticates in front of the hub."""

    def __init__(self, limit: int = 5, window_s: float = 300.0, max_keys: int = 4096) -> None:
        self._limit = limit
        self._window_s = window_s
        self._max_keys = max_keys
        self._fails: dict[str, list[float]] = defaultdict(list)

    def locked(self, key: str, now: float) -> bool:
        recent = [t for t in self._fails.get(key, ()) if now - t < self._window_s]
        # Do not keep an empty bucket: a login always calls this before
        # record_failure, so persisting `key` here (even with no recent failures)
        # would make record_failure's `key not in self._fails` eviction guard dead
        # code and let the map grow one permanent entry per attempted username.
        if recent:
            self._fails[key] = recent
        else:
            self._fails.pop(key, None)
        return len(recent) >= self._limit

    def record_failure(self, key: str, now: float) -> None:
        # Bound the map so a flood of distinct usernames cannot grow it without
        # limit: drop the stalest keys once it is full.
        if key not in self._fails and len(self._fails) >= self._max_keys:
            stale = sorted(self._fails, key=lambda k: max(self._fails[k], default=0.0))[: self._max_keys // 4]
            for k in stale:
                self._fails.pop(k, None)
        self._fails[key].append(now)

    def clear(self, key: str) -> None:
        self._fails.pop(key, None)


def is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


async def identity_from_cookies(cookies: Mapping[str, str], rt: Runtime) -> Identity | None:
    """The signed-in identity for a session cookie, or None. Works for both an
    HTTP request and a WebSocket, which each expose `.cookies`."""
    sid = cookies.get(SESSION_COOKIE)
    if not sid or rt.store is None:
        return None
    row = await rt.store.session_identity(sid)
    if row is None:
        return None
    # An account written before roles existed, or by a future version, is read
    # as the *lesser* role rather than the greater one: an unrecognised value
    # must never be an accidental promotion.
    role = row.get("role")
    return Identity(
        username=str(row["username"]),
        role=ROLE_ADMIN if role == ROLE_ADMIN else ROLE_USER,
        display_name=row.get("display_name"),
    )


async def username_from_cookies(cookies: Mapping[str, str], rt: Runtime) -> str | None:
    """The signed-in username for a session cookie, or None."""
    identity = await identity_from_cookies(cookies, rt)
    return identity.username if identity else None


async def current_identity(request: Request, rt: Runtime) -> Identity | None:
    """The signed-in identity for this request, or None.

    The result is memoised on the request, because the login middleware already
    resolved it for every guarded route and a handler asking again would run the
    same query a second time on the one shared connection."""
    cached = getattr(request.state, "vahub_identity", _UNSET)
    if cached is not _UNSET:
        return cached  # type: ignore[return-value]
    identity = await identity_from_cookies(request.cookies, rt)
    request.state.vahub_identity = identity
    return identity


async def current_username(request: Request, rt: Runtime) -> str | None:
    """The signed-in username for this request, or None."""
    identity = await current_identity(request, rt)
    return identity.username if identity else None


async def is_admin(request: Request, rt: Runtime) -> bool:
    """Whether this request may do the administrative things.

    With the built-in login off, the hub keeps no accounts and the reverse proxy
    is the only gate; everyone who gets through it is an operator, so this is
    true. That is the behaviour every release before roles had, and turning the
    login off must not lock the operator out of their own module management."""
    if not rt.config.web.auth.enabled:
        return True
    identity = await current_identity(request, rt)
    return identity is not None and identity.is_admin


async def require_admin(request: Request, rt: Runtime) -> None:
    """Raise 403 unless this request may do the administrative things.

    403 and not 404: the caller is signed in and this is a real route, so hiding
    it would only make the page harder to debug. The route still exists for
    everyone; what it refuses is the action."""
    if not await is_admin(request, rt):
        raise HTTPException(status_code=403, detail="admin only")


async def issue_session(response: Response, username: str, rt: Runtime) -> None:
    """Mint a fresh session for `username` and attach its cookie to `response`.

    One place builds the cookie, so the flags cannot drift between the login,
    the first-run setup and a password change that has to hand the person back
    a working session after revoking their old ones. The token is 32 random
    bytes from `secrets`, and it is the only thing the browser holds: the server
    can end it at any time by deleting the row."""
    auth = rt.config.web.auth
    sid = secrets.token_urlsafe(32)
    await rt.store.create_session(sid, username, time.time() + auth.session_ttl_s)
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        max_age=int(auth.session_ttl_s),
        httponly=True,
        samesite="strict",
        secure=auth.cookie_secure,
        path="/",
    )


def build_router(rt: Runtime) -> APIRouter:
    router = APIRouter()
    throttle = Throttle()

    @router.get("/me")
    async def me(request: Request) -> JSONResponse:
        """What the page needs to decide between the assistant and a login form,
        and which of its controls to draw at all. Never requires auth; it is how
        the client learns whether it must.

        `is_admin` here is a hint for the interface, not the enforcement. Every
        administrative route checks the role for itself, so a client that lies
        to itself about this only redraws its own buttons."""
        auth = rt.config.web.auth
        if not auth.enabled:
            # No accounts, no roles: whoever the proxy let in is the operator.
            return JSONResponse({"auth": False, "is_admin": True, "role": "admin"})
        setup_required = rt.store is not None and await rt.store.count_users() == 0
        identity = await current_identity(request, rt)
        return JSONResponse(
            {
                "auth": True,
                "setup_required": setup_required,
                "authenticated": identity is not None,
                "username": identity.username if identity else None,
                "display_name": identity.display_name if identity else None,
                "role": identity.role if identity else None,
                "is_admin": bool(identity and identity.is_admin),
            }
        )

    @router.post("/setup")
    async def setup(body: SetupBody, request: Request) -> Response:
        """First-run account creation, straight from the browser. It works only
        while the hub has no account at all: the first visitor claims the owner
        account, and the owner is an admin. Once one exists this returns 409, so
        a stranger who reaches the page later cannot sign themselves up; further
        accounts are created by an admin (in the UI or with `vahub user add`)."""
        check_origin(request, rt.config)
        auth = rt.config.web.auth
        if not auth.enabled or rt.store is None:
            return JSONResponse({"ok": False, "error": "auth_disabled"}, status_code=400)
        if await rt.store.count_users() != 0:
            return JSONResponse({"ok": False, "error": "already_set_up"}, status_code=409)
        if (reason := username_error(body.username)) is not None:
            return JSONResponse({"ok": False, "error": "invalid_username", "detail": reason}, status_code=400)
        if (reason := password_error(body.password)) is not None:
            return JSONResponse({"ok": False, "error": "weak_password", "detail": reason}, status_code=400)
        pw_hash = await asyncio.to_thread(hash_password, body.password)
        created = await rt.store.create_first_user(body.username, pw_hash, body.display_name)
        if not created:
            # Lost a race with another first visitor. Their account now exists,
            # so this one is not the first: setup is over.
            return JSONResponse({"ok": False, "error": "already_set_up"}, status_code=409)
        # Sign the new owner straight in, so setup flows into a working session.
        response = JSONResponse({"ok": True, "username": body.username, "role": "admin"})
        await issue_session(response, body.username, rt)
        return response

    @router.post("/login")
    async def login(body: LoginBody, request: Request) -> Response:
        check_origin(request, rt.config)
        auth = rt.config.web.auth
        if not auth.enabled or rt.store is None:
            return JSONResponse({"ok": False, "error": "auth_disabled"}, status_code=400)

        now = time.time()
        key = body.username.lower()
        if throttle.locked(key, now):
            return JSONResponse({"ok": False, "error": "too_many_attempts"}, status_code=429)

        user = await rt.store.get_user(body.username)
        # Verify even when the user is missing, against a throwaway hash, so the
        # response time does not reveal whether the username exists. scrypt is a
        # deliberately expensive, GIL-holding C call, so it runs in a thread: on
        # the event loop it would freeze every other request for its duration,
        # and a flood of logins would be a denial of service.
        stored = (user or {}).get("password_hash") or _DUMMY_HASH
        ok = await asyncio.to_thread(verify_password, body.password, stored)
        if user is None or user.get("disabled") or not ok:
            throttle.record_failure(key, now)
            return JSONResponse({"ok": False, "error": "invalid_credentials"}, status_code=401)

        throttle.clear(key)
        # Opportunistically upgrade an old hash now that the password is in hand.
        if needs_rehash(stored):
            new_hash = await asyncio.to_thread(hash_password, body.password)
            await rt.store.set_password(body.username, new_hash)

        await rt.store.sweep_sessions()  # drop expired rows so the table stays small
        response = JSONResponse({"ok": True, "username": body.username})
        await issue_session(response, body.username, rt)
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
