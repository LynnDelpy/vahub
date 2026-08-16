"""Storage guarantees that a security property rests on.

Two of these back audit findings: a confirmation must fire exactly once even
under a race, and the database (conversations, tool arguments, the audit log)
must not be readable by other users on the host.
"""

from __future__ import annotations

import asyncio
import os
import stat


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
