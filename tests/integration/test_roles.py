"""Roles: what an admin may do, what a plain user may not, and who manages whom.

The interesting assertions here are the refusals. A role that is only drawn in
the interface is not a boundary, so every test below goes through HTTP with a
real session cookie and checks the server's answer, not the page's.
"""

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
PASSWORD = "correct horse battery"


def _config(state_dir: Path, modules_dir: Path) -> Config:
    return Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "web": {
                "origin_allowlist": [ALLOWED_ORIGIN],
                # Plain-http test client, so the cookie must not be Secure-only.
                "auth": {"enabled": True, "cookie_secure": False},
            },
            "llm": {"provider": "mock"},
            "policy": {"default": "deny", "rules": {}},
        }
    )


@pytest.fixture
async def rt(construct, state_dir: Path, modules_dir: Path):
    from vahub.core.runtime import Runtime

    runtime = construct(
        Runtime, config=_config(state_dir, modules_dir), config_path=modules_dir.parent / "vahub.yaml"
    )
    await runtime.store.open()
    runtime.supervisor.discover()
    try:
        yield runtime
    finally:
        await runtime.store.close()


def _client(rt) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(rt))
    client = httpx.AsyncClient(transport=transport, base_url="http://localhost:8080")
    client.headers["origin"] = ALLOWED_ORIGIN
    return client


@pytest.fixture
async def client(rt):
    async with _client(rt) as c:
        yield c


async def _account(rt, username: str, role: str = "user") -> None:
    await rt.store.create_user(username, hash_password(PASSWORD), username.title(), role=role)


async def _sign_in(client: httpx.AsyncClient, username: str) -> None:
    r = await client.post("/api/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text


@pytest.fixture
async def admin(rt, client):
    await _account(rt, "ada", role="admin")
    await _sign_in(client, "ada")
    return client


@pytest.fixture
async def plain(rt, client):
    # An admin has to exist for the last-admin rules to be about anything.
    await _account(rt, "ada", role="admin")
    await _account(rt, "ben", role="user")
    await _sign_in(client, "ben")
    return client


# --------------------------------------------------------------------------
# who am I
# --------------------------------------------------------------------------
async def test_me_reports_the_role(admin) -> None:
    me = (await admin.get("/api/me")).json()
    assert me["username"] == "ada"
    assert me["role"] == "admin"
    assert me["is_admin"] is True


async def test_a_plain_user_is_not_an_admin(plain) -> None:
    me = (await plain.get("/api/me")).json()
    assert me["role"] == "user"
    assert me["is_admin"] is False


async def test_the_first_account_created_at_setup_is_an_admin(client) -> None:
    r = await client.post("/api/setup", json={"username": "ada", "password": PASSWORD})
    assert r.status_code == 200
    assert (await client.get("/api/me")).json()["is_admin"] is True


async def test_a_role_the_hub_does_not_know_is_read_as_the_lesser_one(rt, client) -> None:
    """A row written by a future version, or by hand, must never be an
    accidental promotion."""
    await _account(rt, "mal", role="user")
    await rt.store.db.execute("UPDATE users SET role='superuser' WHERE username='mal'")
    await _sign_in(client, "mal")
    assert (await client.get("/api/me")).json()["is_admin"] is False
    assert (await client.get("/api/users")).status_code == 403


# --------------------------------------------------------------------------
# what a plain user may still do
# --------------------------------------------------------------------------
async def test_a_plain_user_keeps_the_assistant_and_the_shared_data(plain) -> None:
    assert (await plain.get("/api/pending")).status_code == 200
    assert (await plain.get("/api/settings")).status_code == 200
    assert (await plain.put("/api/locations/home", json={"label": "Home"})).status_code == 200
    assert (await plain.get("/api/schedules")).status_code == 200
    # The installed apps are still readable: the dashboard is built out of them.
    assert (await plain.get("/api/modules")).status_code == 200


async def test_a_plain_user_sees_apps_without_their_configuration(rt, plain, write_manifest) -> None:
    write_manifest("fake")
    rt.supervisor.discover()
    await rt.store.set_module_config("fake", "FAKE_NAME", "a-secret-ish-value")

    body = (await plain.get("/api/modules")).json()
    fake = next(m for m in body["modules"] if m["name"] == "fake")
    assert body["can_manage"] is False
    assert [t["name"] for t in fake["tools"]]  # what it can do is not a secret
    # Which keys are set, why it failed, and whether policy allows it are not
    # for someone who cannot act on any of them.
    assert "config" not in fake
    assert "last_error" not in fake
    assert "has_policy_rule" not in fake


async def test_an_admin_sees_the_operator_half(rt, admin, write_manifest) -> None:
    write_manifest("fake")
    rt.supervisor.discover()
    body = (await admin.get("/api/modules")).json()
    fake = next(m for m in body["modules"] if m["name"] == "fake")
    assert body["can_manage"] is True
    assert "config" in fake and "has_policy_rule" in fake


# --------------------------------------------------------------------------
# what a plain user may not do
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/users", None),
        ("POST", "/api/users", {"username": "eve", "password": PASSWORD}),
        ("POST", "/api/users/ada/password", {"password": PASSWORD}),
        ("POST", "/api/users/ada/role", {"role": "user"}),
        ("POST", "/api/users/ada/enabled", {"enabled": False}),
        ("DELETE", "/api/users/ada", None),
        ("GET", "/api/modules/available", None),
        ("POST", "/api/modules", {"name": "fake"}),
        ("DELETE", "/api/modules/fake", None),
        ("GET", "/api/modules/fake/config", None),
        ("PUT", "/api/modules/fake/config/FAKE_NAME", {"value": "x"}),
        ("DELETE", "/api/modules/fake/config/FAKE_NAME", None),
    ],
)
async def test_the_administrative_routes_refuse_a_plain_user(
    plain, method: str, path: str, body: Any
) -> None:
    r = await plain.request(method, path, json=body)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code} {r.text}"


async def test_a_refusal_does_not_depend_on_the_module_existing(plain) -> None:
    """The role is checked before anything else, so a plain user cannot learn
    which modules are installed by comparing a 403 against a 404."""
    r = await plain.put("/api/modules/nosuchmodule/config/KEY", json={"value": "x"})
    assert r.status_code == 403


# --------------------------------------------------------------------------
# managing accounts
# --------------------------------------------------------------------------
async def test_an_admin_creates_an_account_and_it_can_sign_in(rt, admin) -> None:
    r = await admin.post("/api/users", json={"username": "ben", "password": PASSWORD, "display_name": "Ben"})
    assert r.status_code == 200, r.text
    assert r.json()["user"] == {
        "username": "ben",
        "display_name": "Ben",
        "role": "user",
        "disabled": False,
        "created_at": r.json()["user"]["created_at"],
    }
    async with _client(rt) as other:
        await _sign_in(other, "ben")
        assert (await other.get("/api/me")).json()["role"] == "user"


async def test_a_created_account_defaults_to_the_lesser_role(admin) -> None:
    await admin.post("/api/users", json={"username": "ben", "password": PASSWORD})
    listing = (await admin.get("/api/users")).json()["users"]
    assert next(u for u in listing if u["username"] == "ben")["role"] == "user"


async def test_the_listing_never_carries_a_password_hash(rt, admin) -> None:
    await _account(rt, "ben")
    body = (await admin.get("/api/users")).text
    assert "scrypt" not in body
    assert "password" not in body


async def test_an_admin_can_promote_and_demote(rt, admin) -> None:
    await _account(rt, "ben")
    assert (await admin.post("/api/users/ben/role", json={"role": "admin"})).status_code == 200
    async with _client(rt) as other:
        await _sign_in(other, "ben")
        assert (await other.get("/api/users")).status_code == 200
        # A demotion takes effect on the next request, without ending the
        # session: the role is read from the account, never from the cookie.
        assert (await admin.post("/api/users/ben/role", json={"role": "user"})).status_code == 200
        assert (await other.get("/api/users")).status_code == 403


async def test_disabling_an_account_ends_its_session_at_once(rt, admin) -> None:
    await _account(rt, "ben")
    async with _client(rt) as other:
        await _sign_in(other, "ben")
        assert (await other.get("/api/settings")).status_code == 200
        assert (await admin.post("/api/users/ben/enabled", json={"enabled": False})).status_code == 200
        assert (await other.get("/api/settings")).status_code == 401


async def test_removing_an_account_removes_it(rt, admin) -> None:
    await _account(rt, "ben")
    assert (await admin.delete("/api/users/ben")).status_code == 200
    assert await rt.store.get_user("ben") is None


async def test_creating_a_duplicate_account_is_refused(rt, admin) -> None:
    await _account(rt, "ben")
    r = await admin.post("/api/users", json={"username": "ben", "password": PASSWORD})
    assert r.status_code == 409


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"username": "Ben", "password": PASSWORD}, "invalid_username"),
        ({"username": "ben", "password": "short"}, "weak_password"),
        ({"username": "ben", "password": PASSWORD, "role": "root"}, "invalid_role"),
    ],
)
async def test_a_new_account_is_validated(admin, body: dict, error: str) -> None:
    r = await admin.post("/api/users", json=body)
    assert r.status_code == 400 and r.json()["error"] == error


async def test_account_writes_are_origin_checked(admin) -> None:
    r = await admin.post(
        "/api/users", json={"username": "eve", "password": PASSWORD}, headers={"origin": "http://evil"}
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------
# not locking everyone out
# --------------------------------------------------------------------------
async def test_the_last_admin_cannot_be_demoted_disabled_or_removed(rt, admin) -> None:
    await _account(rt, "ben")  # a plain user does not count as a way back in
    assert (await admin.post("/api/users/ada/role", json={"role": "user"})).status_code == 409
    assert (await admin.post("/api/users/ada/enabled", json={"enabled": False})).status_code == 409
    assert (await admin.delete("/api/users/ada")).status_code == 409
    assert (await admin.get("/api/users")).status_code == 200  # still an admin


async def test_a_second_admin_makes_the_first_removable(rt, admin) -> None:
    await _account(rt, "ben", role="admin")
    assert (await admin.delete("/api/users/ben")).status_code == 200


async def test_an_admin_cannot_remove_or_demote_themselves(rt, admin) -> None:
    await _account(rt, "ben", role="admin")  # so this is not the last-admin rule
    assert (await admin.delete("/api/users/ada")).status_code == 409
    assert (await admin.post("/api/users/ada/role", json={"role": "user"})).status_code == 409
    assert (await admin.post("/api/users/ada/enabled", json={"enabled": False})).status_code == 409


async def test_a_disabled_admin_is_not_a_way_back_in(rt, admin) -> None:
    """count_admins asks who can sign in, not who holds the role: a disabled
    admin cannot undo anything, so it must not license removing the live one."""
    await _account(rt, "ben", role="admin")
    await rt.store.set_user_disabled("ben", True)
    assert (await admin.delete("/api/users/ada")).status_code == 409


# --------------------------------------------------------------------------
# your own password
# --------------------------------------------------------------------------
async def test_anyone_signed_in_changes_their_own_password(rt, plain) -> None:
    r = await plain.post(
        "/api/me/password", json={"current_password": PASSWORD, "password": "a whole new one"}
    )
    assert r.status_code == 200, r.text
    # The session survives, re-issued, so the person is not thrown out.
    assert (await plain.get("/api/me")).json()["username"] == "ben"
    async with _client(rt) as other:
        r = await other.post("/api/login", json={"username": "ben", "password": "a whole new one"})
        assert r.status_code == 200
        r = await other.post("/api/login", json={"username": "ben", "password": PASSWORD})
        assert r.status_code == 401


async def test_changing_your_password_needs_the_current_one(plain) -> None:
    r = await plain.post("/api/me/password", json={"current_password": "wrong", "password": "another"})
    assert r.status_code == 403 and r.json()["error"] == "wrong_password"


async def test_a_changed_password_ends_every_other_session(rt, plain) -> None:
    async with _client(rt) as elsewhere:
        await _sign_in(elsewhere, "ben")
        assert (await elsewhere.get("/api/settings")).status_code == 200
        await plain.post(
            "/api/me/password", json={"current_password": PASSWORD, "password": "a whole new one"}
        )
        assert (await elsewhere.get("/api/settings")).status_code == 401


async def test_guessing_your_own_password_is_throttled(plain) -> None:
    """Verifying a password is 32 MiB of scrypt, so this route is a way to spend
    the machine as well as a way to guess. It is braked like the login is."""
    for _ in range(5):
        r = await plain.post("/api/me/password", json={"current_password": "no", "password": "another"})
        assert r.status_code == 403
    r = await plain.post("/api/me/password", json={"current_password": "no", "password": "another"})
    assert r.status_code == 429


async def test_an_admin_resets_someone_elses_password_but_not_their_own(rt, admin) -> None:
    await _account(rt, "ben")
    r = await admin.post("/api/users/ben/password", json={"password": "reset by an admin"})
    assert r.status_code == 200
    # Their own goes through /api/me/password, which asks for the current one;
    # allowing it here would be a way around that check with a borrowed session.
    r = await admin.post("/api/users/ada/password", json={"password": "sneaky"})
    assert r.status_code == 409


async def test_an_admin_reset_ends_that_accounts_sessions(rt, admin) -> None:
    await _account(rt, "ben")
    async with _client(rt) as other:
        await _sign_in(other, "ben")
        await admin.post("/api/users/ben/password", json={"password": "reset by an admin"})
        assert (await other.get("/api/settings")).status_code == 401


# --------------------------------------------------------------------------
# with the built-in login off
# --------------------------------------------------------------------------
async def test_with_the_login_off_everyone_is_an_operator(construct, state_dir, modules_dir) -> None:
    """The proxy is the gate in that deployment, and the hub keeps no accounts,
    so roles have nobody to divide. Turning the login off must not lock the
    operator out of managing their own apps."""
    from vahub.core.runtime import Runtime

    config = _config(state_dir, modules_dir)
    config.web.auth.enabled = False
    runtime = construct(Runtime, config=config, config_path=modules_dir.parent / "vahub.yaml")
    await runtime.store.open()
    runtime.supervisor.discover()
    try:
        async with _client(runtime) as c:
            assert (await c.get("/api/me")).json() == {"auth": False, "is_admin": True, "role": "admin"}
            assert (await c.get("/api/modules")).json()["can_manage"] is True
    finally:
        await runtime.store.close()
