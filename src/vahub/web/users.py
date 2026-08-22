"""Accounts, from the browser: an admin manages who may sign in.

Until roles existed, accounts were a CLI-only concern and this file did not
exist. The reason for the change is ordinary: a household hub gets a second
person, and telling that person's account holder to SSH in is not a security
boundary, it is an obstacle. So an admin can now add, disable, rename the role
of, and remove an account from the web. `vahub user` still does all of it from
the host, and remains the way back in when nobody can sign in.

What did *not* change, and is the reason this is safe to add:

* Only an admin reaches any route here. The check is the role on the account,
  re-read from the database on every request, never a claim from the client.
* Policy is still a file. Nothing here grants the assistant a capability, alters
  a rule, or changes what any principal may call. An admin account is authority
  over *the web interface*, not over the gate.
* A password is never returned, never logged, and never stored except as a
  scrypt hash. Setting one ends that account's sessions.
* The hub refuses the change that would leave it with no admin who can sign in.
  Locking yourself out of your own hub is not a security property.

Every account change is logged (who did what to whom, never a password) so the
service log shows how the set of people with access got to be what it is.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import (
    ROLE_ADMIN,
    ROLE_USER,
    hash_password,
    password_error,
    role_error,
    username_error,
    verify_password,
)
from . import auth as web_auth
from .auth import Throttle
from .security import check_origin

if TYPE_CHECKING:
    from ..core.runtime import Runtime

log = structlog.get_logger(__name__)

# Matches auth.USERNAME_RE, as a path-parameter pattern so a malformed name is
# refused by the router before any handler or query sees it.
_USERNAME = r"^[a-z0-9][a-z0-9_.-]{1,31}$"


class CreateUserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str | None = Field(default=None, max_length=80)
    role: str = Field(default=ROLE_USER, max_length=16)


class PasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=1024)


class OwnPasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=1024)
    password: str = Field(min_length=1, max_length=1024)


class RoleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(max_length=16)


class EnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class DisplayNameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=80)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """An account as the browser may see it. The password hash is in the row
    this came from, so the shape is built by naming fields rather than by
    deleting them: a column added later cannot leak by being forgotten."""
    return {
        "username": row.get("username"),
        "display_name": row.get("display_name"),
        "role": ROLE_ADMIN if row.get("role") == ROLE_ADMIN else ROLE_USER,
        "disabled": bool(row.get("disabled")),
        "created_at": row.get("created_at"),
    }


def _bad(error: str, detail: str | None = None, status: int = 400) -> JSONResponse:
    body: dict[str, Any] = {"ok": False, "error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


def build_router(rt: Runtime) -> APIRouter:
    router = APIRouter()
    # Account changes are read-modify-write across two statements (check the
    # last-admin invariant, then change the row). Two admins acting at once
    # could each see one remaining admin and each remove it, so the whole set of
    # writes is serialised. It is a single-process hub; a lock is enough.
    lock = asyncio.Lock()
    # Verifying a password is a deliberate 32 MiB of scrypt, so the one route
    # here that verifies one is braked like the login is. Without this, a signed
    # in account could spend the hub's memory and CPU by guessing at its own
    # password in a loop, and could guess at it without limit besides.
    own_password = Throttle()

    async def _actor(request: Request) -> str:
        who = await web_auth.current_username(request, rt)
        # With the built-in login off there is no account to name, and the proxy
        # subject is a header anyone reaching the hub directly can set, so it is
        # recorded as what it is rather than as an identity.
        return who or "operator"

    async def _last_admin_would_go(username: str) -> bool:
        """Whether removing this account's admin rights leaves nobody able to
        administer the hub from the web."""
        return await rt.store.count_admins(excluding=username) == 0

    # --- the signed-in person's own account -------------------------------
    @router.post("/me/password")
    async def change_own_password(body: OwnPasswordBody, request: Request) -> JSONResponse:
        """Change your own password. Anyone signed in may do this, and only for
        themselves. The current password is required, so a borrowed session (an
        unlocked laptop, a stolen cookie) cannot be turned into a permanent
        account takeover by locking the owner out."""
        check_origin(request, rt.config)
        identity = await web_auth.current_identity(request, rt)
        if identity is None:
            # Only reachable with the built-in login off, where there is no
            # account to change; say so instead of pretending to succeed.
            return _bad("no_account", "the built-in login is off; there is no password to change")
        now = time.time()
        if own_password.locked(identity.username, now):
            return _bad("too_many_attempts", "wait a few minutes", status=429)
        user = await rt.store.get_user(identity.username)
        if user is None:
            return _bad("no_account", status=404)
        ok = await asyncio.to_thread(
            verify_password, body.current_password, str(user.get("password_hash") or "")
        )
        if not ok:
            own_password.record_failure(identity.username, now)
            log.warning("account_password_change_refused", who=identity.username, reason="wrong_current")
            return _bad("wrong_password", "that is not your current password", status=403)
        own_password.clear(identity.username)
        if (reason := password_error(body.password)) is not None:
            return _bad("weak_password", reason)

        pw_hash = await asyncio.to_thread(hash_password, body.password)
        await rt.store.set_password(identity.username, pw_hash)
        # Every session for this account ends, including this one: a password
        # change is how you get rid of a session you do not trust. The browser
        # that asked is handed a fresh one so it is not signed out mid-sentence.
        await rt.store.drop_user_sessions(identity.username)
        response = JSONResponse({"ok": True})
        await web_auth.issue_session(response, identity.username, rt)
        log.info("account_password_changed", who=identity.username, by=identity.username)
        return response

    # --- managing accounts (admin only) -----------------------------------
    @router.get("/users")
    async def list_users(request: Request) -> JSONResponse:
        await web_auth.require_admin(request, rt)
        rows = [_public(row) for row in await rt.store.list_users()]
        return JSONResponse({"users": rows, "admins": await rt.store.count_admins()})

    @router.post("/users")
    async def create_user(body: CreateUserBody, request: Request) -> JSONResponse:
        await web_auth.require_admin(request, rt)
        check_origin(request, rt.config)
        if (reason := username_error(body.username)) is not None:
            return _bad("invalid_username", reason)
        if (reason := password_error(body.password)) is not None:
            return _bad("weak_password", reason)
        if (reason := role_error(body.role)) is not None:
            return _bad("invalid_role", reason)
        pw_hash = await asyncio.to_thread(hash_password, body.password)
        async with lock:
            if await rt.store.get_user(body.username) is not None:
                return _bad("already_exists", body.username, status=409)
            await rt.store.create_user(body.username, pw_hash, body.display_name, role=body.role)
        log.info("account_created", who=body.username, role=body.role, by=await _actor(request))
        user = await rt.store.get_user(body.username)
        return JSONResponse({"ok": True, "user": _public(user or {})})

    @router.post("/users/{username}/password")
    async def set_password(
        body: PasswordBody, request: Request, username: str = Path(pattern=_USERNAME)
    ) -> JSONResponse:
        """An admin resetting somebody else's password, for the person who
        forgot theirs. It ends that account's sessions, so the reset is also how
        you evict a session you do not trust.

        Changing your *own* password goes through /api/me/password, which asks
        for the current one. An admin resetting their own here would be a way to
        skip that check with a borrowed session, so it is refused."""
        await web_auth.require_admin(request, rt)
        check_origin(request, rt.config)
        actor = await _actor(request)
        if username == actor:
            return _bad("use_own_endpoint", "change your own password at /api/me/password", status=409)
        if (reason := password_error(body.password)) is not None:
            return _bad("weak_password", reason)
        pw_hash = await asyncio.to_thread(hash_password, body.password)
        async with lock:
            if not await rt.store.set_password(username, pw_hash):
                return _bad("no_such_user", username, status=404)
            await rt.store.drop_user_sessions(username)
        log.info("account_password_reset", who=username, by=actor)
        return JSONResponse({"ok": True, "username": username})

    @router.post("/users/{username}/role")
    async def set_role(
        body: RoleBody, request: Request, username: str = Path(pattern=_USERNAME)
    ) -> JSONResponse:
        await web_auth.require_admin(request, rt)
        check_origin(request, rt.config)
        if (reason := role_error(body.role)) is not None:
            return _bad("invalid_role", reason)
        actor = await _actor(request)
        if username == actor and body.role != ROLE_ADMIN:
            # Not a security rule, a usability one: demoting yourself is almost
            # always a misclick, and the hub cannot undo it for you.
            return _bad("cannot_demote_self", "ask another admin to change your own role", status=409)
        async with lock:
            existing = await rt.store.get_user(username)
            if existing is None:
                return _bad("no_such_user", username, status=404)
            if body.role != ROLE_ADMIN and await _last_admin_would_go(username):
                return _bad("last_admin", "this is the only admin who can sign in", status=409)
            await rt.store.set_user_role(username, body.role)
        # The role is re-read from the account on every request, so this takes
        # effect on their next one; their sessions stay valid deliberately, as a
        # demotion is not a reason to throw somebody out of a conversation.
        log.info("account_role_changed", who=username, role=body.role, by=actor)
        return JSONResponse({"ok": True, "username": username, "role": body.role})

    @router.post("/users/{username}/enabled")
    async def set_enabled(
        body: EnabledBody, request: Request, username: str = Path(pattern=_USERNAME)
    ) -> JSONResponse:
        await web_auth.require_admin(request, rt)
        check_origin(request, rt.config)
        actor = await _actor(request)
        if username == actor and not body.enabled:
            return _bad("cannot_disable_self", "you would be signed out immediately", status=409)
        async with lock:
            if await rt.store.get_user(username) is None:
                return _bad("no_such_user", username, status=404)
            if not body.enabled and await _last_admin_would_go(username):
                return _bad("last_admin", "this is the only admin who can sign in", status=409)
            if not await rt.store.set_user_disabled(username, not body.enabled):
                return _bad("no_such_user", username, status=404)
            if not body.enabled:
                # Disabling already stops sessions resolving; dropping the rows
                # makes that visible in the table rather than implicit in a join.
                await rt.store.drop_user_sessions(username)
        log.info("account_enabled" if body.enabled else "account_disabled", who=username, by=actor)
        return JSONResponse({"ok": True, "username": username, "disabled": not body.enabled})

    @router.post("/users/{username}/display-name")
    async def set_display_name(
        body: DisplayNameBody, request: Request, username: str = Path(pattern=_USERNAME)
    ) -> JSONResponse:
        await web_auth.require_admin(request, rt)
        check_origin(request, rt.config)
        async with lock:
            user = await rt.store.get_user(username)
            if user is None:
                return _bad("no_such_user", username, status=404)
            await rt.store.set_display_name(username, body.display_name)
        return JSONResponse({"ok": True, "username": username, "display_name": body.display_name})

    @router.delete("/users/{username}")
    async def remove_user(request: Request, username: str = Path(pattern=_USERNAME)) -> JSONResponse:
        await web_auth.require_admin(request, rt)
        check_origin(request, rt.config)
        actor = await _actor(request)
        if username == actor:
            return _bad("cannot_remove_self", "ask another admin to remove your account", status=409)
        async with lock:
            if await rt.store.get_user(username) is None:
                return _bad("no_such_user", username, status=404)
            if await _last_admin_would_go(username):
                return _bad("last_admin", "this is the only admin who can sign in", status=409)
            removed = await rt.store.delete_user(username)
        if not removed:
            return _bad("no_such_user", username, status=404)
        log.info("account_removed", who=username, by=actor)
        # What the account left behind (schedules it created, places it saved)
        # stays: it is the household's data, and deleting a person should not
        # silently stop the morning routine they set up.
        return JSONResponse({"ok": True, "username": username})

    return router


__all__ = ["build_router"]
