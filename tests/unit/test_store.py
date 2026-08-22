"""Storage guarantees that a security property rests on.

Two of these back audit findings: a confirmation must fire exactly once even
under a race, and the database (conversations, tool arguments, the audit log)
must not be readable by other users on the host.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time

import pytest


async def _make_pending(store, pid: str = "p1") -> None:
    await store.create_pending(pid, "agent", "home", "lock_unlock", {"entity_id": "lock.front"}, 60.0)


async def test_consume_pending_is_single_use(store) -> None:
    await _make_pending(store)
    assert await store.consume_pending("p1") is True
    # A second consume of the same id must not succeed: the frozen call has
    # already been claimed.
    assert await store.consume_pending("p1") is False


async def test_concurrent_consume_fires_exactly_once(store) -> None:
    # Two confirmations of one pending id arriving together (a double click, or an
    # attacker firing twice) must dispatch the destructive call once, not twice.
    await _make_pending(store, "race")
    results = await asyncio.gather(*(store.consume_pending("race") for _ in range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7


async def test_consume_pending_ignores_a_missing_or_non_pending_row(store) -> None:
    assert await store.consume_pending("nope") is False
    await _make_pending(store, "expired")
    await store.set_pending_status("expired", "expired")
    assert await store.consume_pending("expired") is False


async def test_database_is_not_world_or_group_readable(store) -> None:
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode & 0o077 == 0, f"db mode {oct(mode)} exposes the audit log to other users"


async def test_upsert_location_partial_update_keeps_other_fields(store) -> None:
    # Renaming the label of a place that already has coordinates must not wipe
    # the coordinates to NULL.
    await store.upsert_location("home", label="Home", latitude=47.4, longitude=9.4)
    await store.upsert_location("home", label="House")  # no coordinates this time
    loc = await store.get_location("home")
    assert loc["label"] == "House"
    assert loc["latitude"] == 47.4 and loc["longitude"] == 9.4


async def test_the_role_migration_promotes_accounts_that_predate_roles(tmp_path) -> None:
    """An account created before roles existed had every right there was, so the
    migration must not silently take that away. Build a v3 database with two
    accounts in it, then open it with the current code and check both are
    admins."""
    from vahub.storage import store as store_mod

    path = tmp_path / "old.db"
    v3_only = store_mod.MIGRATIONS[:3]
    assert v3_only[-1][0] == 3, "this test pins the shape of the migration list"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_mod, "MIGRATIONS", v3_only)
        old = store_mod.Store(path)
        await old.open()
        assert await old.schema_version() == 3
        # The v3 users table has no role column, so this is the insert of the day.
        for name in ("ada", "ben"):
            await old.db.execute(
                "INSERT INTO users(username, password_hash, display_name, created_at, disabled)"
                " VALUES(?,?,?,?,0)",
                (name, "scrypt$1$1$1$x$y", name.title(), 1.0),
            )
        await old.close()

    upgraded = store_mod.Store(path)
    await upgraded.open()
    try:
        assert await upgraded.schema_version() == store_mod.SCHEMA_VERSION
        assert {u["username"]: u["role"] for u in await upgraded.list_users()} == {
            "ada": "admin",
            "ben": "admin",
        }
        assert await upgraded.count_admins() == 2
    finally:
        await upgraded.close()


async def test_a_new_account_is_a_plain_user_unless_asked_otherwise(store) -> None:
    await store.create_user("ben", "h", None)
    await store.create_user("ada", "h", None, role="admin")
    assert {u["username"]: u["role"] for u in await store.list_users()} == {
        "ben": "user",
        "ada": "admin",
    }


async def test_count_admins_ignores_the_disabled_and_the_excluded(store) -> None:
    await store.create_user("ada", "h", None, role="admin")
    await store.create_user("bea", "h", None, role="admin")
    await store.create_user("ben", "h", None)
    assert await store.count_admins() == 2
    assert await store.count_admins(excluding="ada") == 1
    await store.set_user_disabled("bea", True)
    # A disabled admin cannot sign in, so it is not a way back into the hub.
    assert await store.count_admins(excluding="ada") == 0


async def test_session_identity_carries_the_current_role(store) -> None:
    await store.create_user("ada", "h", "Ada", role="admin")
    await store.create_session("sid", "ada", time.time() + 60)
    assert (await store.session_identity("sid"))["role"] == "admin"
    # Demoting takes effect on the next lookup: the role lives on the account,
    # never in the session.
    await store.set_user_role("ada", "user")
    assert (await store.session_identity("sid"))["role"] == "user"
    await store.set_user_disabled("ada", True)
    assert await store.session_identity("sid") is None


async def test_count_users_includes_disabled(store) -> None:
    # "Setup required" means no account at all, so a disabled account still counts.
    await store.create_user("lynn", "x", None)
    await store.set_user_disabled("lynn", True)
    assert await store.count_users() == 1


async def test_sweep_sessions_drops_expired(store) -> None:
    import time

    await store.create_user("lynn", "x", None)
    await store.create_session("live", "lynn", time.time() + 100)
    await store.create_session("dead", "lynn", time.time() - 1)
    assert await store.sweep_sessions() == 1
    assert await store.session_user("live") == "lynn"
    assert await store.session_user("dead") is None


async def test_recent_tool_calls_filters_before_the_limit(store) -> None:
    # One old denial, then a wall of newer allowed calls. `--denied -n 1` must
    # find the denial, not report nothing because it fell outside the last N.
    await store.record_tool_call("agent", "door", "unlock", {}, "deny", "denied", 1.0)
    for _ in range(20):
        await store.record_tool_call("agent", "time", "now", {}, "allow", "ok", 1.0)

    denied = await store.recent_tool_calls(limit=1, decision="deny")
    assert len(denied) == 1 and denied[0]["tool"] == "unlock"

    # Filtering by principal likewise applies in SQL, before the limit.
    await store.record_tool_call("scheduler", "time", "now", {}, "allow", "ok", 1.0)
    sched = await store.recent_tool_calls(limit=5, principal="scheduler")
    assert [r["principal"] for r in sched] == ["scheduler"]


async def test_module_config_roundtrips_and_lists_keys_without_values(store) -> None:
    # The values feed the supervisor's environment builder; the browser only ever
    # learns which keys are set, never the token itself.
    await store.set_module_config("github", "TOKEN", "a-fake-token")
    await store.set_module_config("github", "BASE_URL", "https://ghe.example")
    assert await store.module_config("github") == {"TOKEN": "a-fake-token", "BASE_URL": "https://ghe.example"}
    assert await store.module_config_keys("github") == ["BASE_URL", "TOKEN"]


async def test_module_config_is_scoped_per_module(store) -> None:
    await store.set_module_config("github", "TOKEN", "gh")
    await store.set_module_config("gitlab", "TOKEN", "gl")
    snapshot = await store.all_module_config()
    assert snapshot == {"github": {"TOKEN": "gh"}, "gitlab": {"TOKEN": "gl"}}


async def test_module_config_update_and_delete(store) -> None:
    await store.set_module_config("email", "PASSWORD", "one")
    await store.set_module_config("email", "PASSWORD", "two")  # upsert, not a second row
    assert await store.module_config("email") == {"PASSWORD": "two"}
    assert await store.delete_module_config("email", "PASSWORD") is True
    assert await store.module_config("email") == {}
    assert await store.delete_module_config("email", "PASSWORD") is False


async def test_delete_all_module_config_drops_every_key(store) -> None:
    await store.set_module_config("gitlab", "TOKEN", "t")
    await store.set_module_config("gitlab", "BASE_URL", "u")
    assert await store.delete_all_module_config("gitlab") == 2
    assert await store.module_config("gitlab") == {}


async def test_module_config_never_leaks_through_app_settings(store) -> None:
    # A module secret and a plain preference share no table: the preferences view
    # (all_settings) must never surface a stored token.
    await store.set_module_config("github", "TOKEN", "a-fake-token")
    await store.set_setting("units", "metric")
    assert await store.all_settings() == {"units": "metric"}


async def test_vacuum_into_writes_a_private_backup(store, tmp_path) -> None:
    import os
    import stat

    dest = tmp_path / "backups" / "copy.db"
    await store.vacuum_into(dest)
    # The backup is a full copy of a secret-bearing DB, so it and its directory
    # must be private, not left at the umask default.
    assert stat.S_IMODE(os.stat(dest).st_mode) & 0o077 == 0
    assert stat.S_IMODE(os.stat(dest.parent).st_mode) & 0o077 == 0
