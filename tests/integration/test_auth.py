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
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost:8080"
    ) as c:
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
