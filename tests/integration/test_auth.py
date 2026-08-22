"""The built-in login: guarding routes, the login flow, and revocation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from vahub.auth import hash_password
from vahub.config.models import Config
from vahub.web.app import create_app

pytestmark = pytest.mark.integration

ALLOWED_ORIGIN = "http://localhost:8080"


def _config(state_dir: Path, modules_dir: Path, **auth: Any) -> Config:
    return Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "web": {
                "origin_allowlist": [ALLOWED_ORIGIN],
                # Plain-http test client, so the cookie must not be Secure-only.
                "auth": {"enabled": True, "cookie_secure": False, **auth},
            },
            "llm": {"provider": "mock"},
            "policy": {"default": "deny", "rules": {}},
        }
    )


@pytest.fixture
async def rt(construct, state_dir: Path, modules_dir: Path, request):
    from vahub.core.runtime import Runtime

    overrides = getattr(request, "param", {}) or {}
    config = _config(state_dir, modules_dir, **overrides)
    runtime = construct(Runtime, config=config, config_path=modules_dir.parent / "vahub.yaml")
    await runtime.store.open()
    runtime.supervisor.discover()
    try:
        yield runtime
    finally:
        await runtime.store.close()


@pytest.fixture
async def client(rt):
    transport = httpx.ASGITransport(app=create_app(rt))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as c:
        c.headers["origin"] = ALLOWED_ORIGIN
        yield c


async def _add_user(rt, username: str = "lynn", password: str = "correct horse") -> None:
    await rt.store.create_user(username, hash_password(password), "Lynn")


async def test_api_requires_login_when_enabled(client) -> None:
    r = await client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 401
    assert r.json()["error"] == "auth_required"


async def test_page_and_me_are_reachable_without_login(client) -> None:
    assert (await client.get("/")).status_code == 200
    me = await client.get("/api/me")
    assert me.status_code == 200
    assert me.json() == {
        "auth": True,
        "setup_required": True,  # no accounts created yet
        "authenticated": False,
        "username": None,
        "display_name": None,
        "role": None,
        "is_admin": False,
    }


async def test_login_then_reach_the_api(client, rt) -> None:
    await _add_user(rt)
    bad = await client.post("/api/login", json={"username": "lynn", "password": "wrong"})
    assert bad.status_code == 401

    ok = await client.post("/api/login", json={"username": "lynn", "password": "correct horse"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    # The session cookie now lets the guarded route through.
    assert (await client.get("/api/pending")).status_code == 200
    me = await client.get("/api/me")
    assert me.json()["authenticated"] is True and me.json()["username"] == "lynn"


async def test_logout_and_revocation(client, rt) -> None:
    await _add_user(rt)
    await client.post("/api/login", json={"username": "lynn", "password": "correct horse"})
    assert (await client.get("/api/pending")).status_code == 200

    await client.post("/api/logout")
    assert (await client.get("/api/pending")).status_code == 401


async def test_disabled_account_cannot_use_an_existing_session(client, rt) -> None:
    await _add_user(rt)
    await client.post("/api/login", json={"username": "lynn", "password": "correct horse"})
    assert (await client.get("/api/pending")).status_code == 200
    # Revoking the account invalidates the live session immediately.
    await rt.store.set_user_disabled("lynn", True)
    assert (await client.get("/api/pending")).status_code == 401


async def test_login_is_throttled(client, rt) -> None:
    await _add_user(rt)
    for _ in range(5):
        await client.post("/api/login", json={"username": "lynn", "password": "no"})
    blocked = await client.post("/api/login", json={"username": "lynn", "password": "no"})
    assert blocked.status_code == 429


async def test_metrics_is_reachable_without_a_session(client) -> None:
    # A scraper has no login; /metrics must not be caught by the auth guard (it is
    # still origin-checked and the proxy 404s it for clients).
    r = await client.get("/metrics")
    assert r.status_code == 200


async def test_setup_required_is_false_when_the_only_account_is_disabled(client, rt) -> None:
    await _add_user(rt)
    await rt.store.set_user_disabled("lynn", True)
    me = (await client.get("/api/me")).json()
    # An account still exists, it is just disabled: re-enable it, do not "set up".
    assert me["setup_required"] is False


# --------------------------------------------------------------------------
# first-run setup and the owner management surface
# --------------------------------------------------------------------------
async def test_first_visitor_setup_creates_the_owner_and_signs_in(client) -> None:
    r = await client.post(
        "/api/setup",
        json={"username": "lynn", "password": "correct horse", "display_name": "Lynn"},
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    # The response signs the new owner straight in, so the guarded API is reachable.
    assert (await client.get("/api/pending")).status_code == 200
    me = (await client.get("/api/me")).json()
    assert me["authenticated"] is True and me["username"] == "lynn"
    assert me["setup_required"] is False


async def test_setup_is_refused_once_an_account_exists(client, rt) -> None:
    await _add_user(rt)
    r = await client.post("/api/setup", json={"username": "someone", "password": "correct horse"})
    assert r.status_code == 409 and r.json()["error"] == "already_set_up"


async def test_setup_rejects_a_weak_password(client) -> None:
    r = await client.post("/api/setup", json={"username": "lynn", "password": "short"})
    assert r.status_code == 400 and r.json()["error"] == "weak_password"


async def test_setup_rejects_a_bad_username(client) -> None:
    r = await client.post("/api/setup", json={"username": "Bad Name", "password": "correct horse"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_username"


async def test_setup_is_origin_checked(client) -> None:
    r = await client.post(
        "/api/setup",
        json={"username": "lynn", "password": "correct horse"},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


async def test_module_management_and_tool_calls_require_login(client) -> None:
    # The owner surface (module management, reading module data for a card) is
    # guarded by the same login as the rest of the API.
    assert (await client.get("/api/modules")).status_code == 401
    assert (await client.post("/api/tools/fake/echo", json={"args": {}})).status_code == 401


async def test_account_management_requires_login_before_it_requires_a_role(client) -> None:
    """The role check is inside the handler, so it would be reached by an
    unauthenticated caller if the login middleware ever stopped covering these
    paths. 401, not 403: nobody is signed in yet."""
    assert (await client.get("/api/users")).status_code == 401
    assert (
        await client.post("/api/users", json={"username": "eve", "password": "x" * 12})
    ).status_code == 401
    assert (await client.delete("/api/users/lynn")).status_code == 401
    assert (
        await client.post("/api/me/password", json={"current_password": "a", "password": "b" * 12})
    ).status_code == 401


def test_throttle_prunes_empty_buckets_and_stays_bounded() -> None:
    # The login path calls locked() before record_failure() for the same key, so
    # locked() must not persist an empty bucket, or the max_keys eviction becomes
    # dead code and the map grows one entry per attempted username forever.
    import time

    from vahub.web.auth import Throttle

    t = Throttle(limit=5, window_s=300.0, max_keys=8)
    now = time.time()
    for i in range(50):
        assert t.locked(f"ghost{i}", now) is False  # never-failing usernames
    assert len(t._fails) == 0  # no empty buckets accumulated

    for i in range(100):
        t.record_failure(f"user{i}", now)  # a flood of distinct failing usernames
    assert len(t._fails) <= 8  # bounded by max_keys, not unbounded
