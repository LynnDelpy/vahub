"""SQLite persistence: one connection, WAL, explicit ordered migrations.

The audit log (`tool_calls`) is why this layer exists. When a light turns on at
three in the morning, that table says whether the agent, the scheduler or a
person did it, with which arguments, and under which policy decision.

Two decisions worth stating.

The connection runs in autocommit mode (isolation_level=None). Coroutines share
one connection, and with implicit transactions one coroutine's commit() would
publish another coroutine's half-written work. In autocommit mode each statement
stands alone; the only multi-statement unit is a migration, which brackets
itself with BEGIN and COMMIT.

The schema is versioned in a table instead of being recreated with CREATE TABLE
IF NOT EXISTS on every start. This database outlives any single release: a
column added next year has to arrive on a database that already holds a year of
history, and only an ordered, recorded migration does that.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import TracebackType
from typing import Any

import aiosqlite

# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------
# Append-only. A released migration is never edited, because databases in the
# field have already applied it; correcting a mistake means adding the next one.
# Each entry is a list of single statements rather than one script, because
# sqlite3.executescript() commits any open transaction and would defeat the
# BEGIN/COMMIT bracket that makes a migration all-or-nothing.

_V1: tuple[str, ...] = (
    """
    CREATE TABLE conversations (
        id          TEXT PRIMARY KEY,
        created_at  REAL NOT NULL,
        last_at     REAL NOT NULL
    )
    """,
    # No foreign key to conversations: a message, and above all an audit row,
    # must never be rejected because a parent row was pruned or never written.
    """
    CREATE TABLE messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        role            TEXT NOT NULL,
        content         TEXT,
        created_at      REAL NOT NULL
    )
    """,
    """
    CREATE TABLE tool_calls (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL NOT NULL,
        principal   TEXT NOT NULL,   -- agent | scheduler | a human subject
        module      TEXT NOT NULL,
        tool        TEXT NOT NULL,
        args        TEXT,            -- JSON, with manifest-declared keys redacted
        decision    TEXT NOT NULL,   -- allow | allow-confirmed | deny | confirm
        result      TEXT,            -- ok | timeout | error | tool_error | ...
        duration_ms REAL
    )
    """,
    """
    CREATE TABLE pending_calls (
        id          TEXT PRIMARY KEY,
        ts          REAL NOT NULL,
        expires_at  REAL NOT NULL,
        principal   TEXT NOT NULL,
        module      TEXT NOT NULL,
        tool        TEXT NOT NULL,
        args        TEXT NOT NULL,   -- frozen JSON: what gets executed on confirm
        status      TEXT NOT NULL    -- pending | confirmed | expired | cancelled
    )
    """,
    """
    CREATE TABLE module_state (
        module      TEXT PRIMARY KEY,
        state       TEXT NOT NULL,
        last_error  TEXT,
        updated_at  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE budget_usage (
        day         TEXT PRIMARY KEY,  -- YYYY-MM-DD in the hub timezone
        tokens      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_tool_calls_ts ON tool_calls(ts)",
    "CREATE INDEX idx_tool_calls_module ON tool_calls(module, tool)",
    "CREATE INDEX idx_messages_conv ON messages(conversation_id, id)",
    "CREATE INDEX idx_pending_status ON pending_calls(status, expires_at)",
)

MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = ((1, _V1),)

SCHEMA_VERSION = MIGRATIONS[-1][0]


class Store:
    """Async access to the hub database. Open it once and share the instance."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._db: aiosqlite.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store not open")
        return self._db

    async def open(self) -> None:
        if self._db is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path, isolation_level=None)
        db.row_factory = aiosqlite.Row
        # WAL lets a reader (the status page, a backup) run while a writer works.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        # Rather than surfacing "database is locked" the moment a VACUUM INTO
        # backup overlaps a write.
        await db.execute("PRAGMA busy_timeout=5000")
        self._db = db
        await self._migrate()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Store:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # --- schema ------------------------------------------------------------
    async def _migrate(self) -> None:
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        current = await self.schema_version()
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            await self.db.execute("BEGIN")
            try:
                for statement in statements:
                    await self.db.execute(statement)
                await self.db.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(?,?)",
                    (version, time.time()),
                )
            except Exception:
                # Half a migration is worse than none: leave the database on the
                # previous version so the next start can retry it cleanly.
                await self.db.execute("ROLLBACK")
                raise
            await self.db.execute("COMMIT")

    async def schema_version(self) -> int:
        cur = await self.db.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
        row = await cur.fetchone()
        return int(row["v"]) if row else 0

    async def vacuum_into(self, dest: Path) -> Path:
        """Write a consistent copy of the database to `dest`.

        Copying the file while WAL is active yields a torn backup (the committed
        tail lives in the -wal file). VACUUM INTO takes a read transaction and
        writes a complete, compacted database, so the result is always loadable.
        """
        target = Path(dest)
        if target.exists():
            raise FileExistsError(f"backup target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.db.execute("VACUUM INTO ?", (str(target),))
        return target

    # --- audit log ---------------------------------------------------------
    async def record_tool_call(
        self,
        principal: str,
        module: str,
        tool: str,
        args: dict[str, Any],
        decision: str,
        result: str | None,
        duration_ms: float | None,
    ) -> None:
        """Append one row to the audit log. `args` is expected to be redacted by
        the caller, which is the only place that knows the module's manifest."""
        # default=str: an argument that is not JSON-serialisable must not cost us
        # the audit row. Losing the record of a call is worse than an approximate
        # rendering of one argument.
        payload = json.dumps(args, default=str, sort_keys=True)
        await self.db.execute(
            "INSERT INTO tool_calls(ts, principal, module, tool, args, decision, result, duration_ms)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), principal, module, tool, payload, decision, result, duration_ms),
        )

    async def recent_tool_calls(self, limit: int = 100) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT id, ts, principal, module, tool, args, decision, result, duration_ms"
            " FROM tool_calls ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def tool_calls_since(self, ts: float, limit: int = 1000) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT id, ts, principal, module, tool, args, decision, result, duration_ms"
            " FROM tool_calls WHERE ts >= ? ORDER BY id DESC LIMIT ?",
            (ts, max(1, int(limit))),
        )
        return [dict(row) for row in await cur.fetchall()]

    # --- pending confirmations ---------------------------------------------
    async def create_pending(
        self,
        pid: str,
        principal: str,
        module: str,
        tool: str,
        args: dict[str, Any],
        ttl_s: float,
    ) -> None:
        """Freeze the arguments of a call awaiting confirmation.

        The frozen copy is the point: what the user approves is what runs, not
        whatever the model has in its context by the time the answer comes back.
        """
        now = time.time()
        await self.db.execute(
            "INSERT INTO pending_calls(id, ts, expires_at, principal, module, tool, args, status)"
            " VALUES(?,?,?,?,?,?,?,'pending')",
            (pid, now, now + ttl_s, principal, module, tool, json.dumps(args, sort_keys=True)),
        )

    async def get_pending(self, pid: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM pending_calls WHERE id=?", (pid,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_pending_status(self, pid: str, status: str) -> None:
        await self.db.execute("UPDATE pending_calls SET status=? WHERE id=?", (status, pid))

    async def list_pending(self) -> list[dict[str, Any]]:
        """Only confirmations that can still be acted on. The args column is not
        selected: this feeds a UI, and the caller does not need the payload."""
        cur = await self.db.execute(
            "SELECT id, ts, expires_at, principal, module, tool, status FROM pending_calls"
            " WHERE status='pending' AND expires_at > ? ORDER BY ts DESC",
            (time.time(),),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def expire_pending(self) -> int:
        """Mark timed-out confirmations expired. Without this they stay 'pending'
        for ever and a stale row could be confirmed by a later caller."""
        cur = await self.db.execute(
            "UPDATE pending_calls SET status='expired' WHERE status='pending' AND expires_at <= ?",
            (time.time(),),
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # --- conversations -----------------------------------------------------
    async def upsert_conversation(self, cid: str) -> None:
        now = time.time()
        await self.db.execute(
            "INSERT INTO conversations(id, created_at, last_at) VALUES(?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET last_at=excluded.last_at",
            (cid, now, now),
        )

    async def add_message(self, cid: str, role: str, content: str | None) -> None:
        await self.db.execute(
            "INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
            (cid, role, content, time.time()),
        )

    async def messages(self, cid: str, limit: int = 200) -> list[dict[str, Any]]:
        """Oldest first, capped. The cap is the useful part: a long-running
        conversation must not be able to load unbounded rows into a reply."""
        cur = await self.db.execute(
            "SELECT id, role, content, created_at FROM messages"
            " WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
            (cid, max(1, int(limit))),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        rows.reverse()
        return rows

    async def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT id, created_at, last_at FROM conversations ORDER BY last_at DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [dict(row) for row in await cur.fetchall()]

    # --- module state ------------------------------------------------------
    async def save_module_state(self, module: str, state: str, last_error: str | None) -> None:
        await self.db.execute(
            "INSERT INTO module_state(module, state, last_error, updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(module) DO UPDATE SET state=excluded.state,"
            " last_error=excluded.last_error, updated_at=excluded.updated_at",
            (module, state, last_error, time.time()),
        )

    async def module_states(self) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT module, state, last_error, updated_at FROM module_state ORDER BY module"
        )
        return [dict(row) for row in await cur.fetchall()]

    # --- budget ------------------------------------------------------------
    async def add_tokens(self, day: str, tokens: int) -> int:
        """Add to the day's usage and return the new total.

        One statement with RETURNING, so two turns finishing at the same moment
        cannot both read a stale total and both conclude they are under budget.
        """
        cur = await self.db.execute(
            "INSERT INTO budget_usage(day, tokens) VALUES(?,?)"
            " ON CONFLICT(day) DO UPDATE SET tokens = tokens + excluded.tokens"
            " RETURNING tokens",
            (day, int(tokens)),
        )
        row = await cur.fetchone()
        return int(row["tokens"]) if row else 0

    async def tokens_today(self, day: str) -> int:
        cur = await self.db.execute("SELECT tokens FROM budget_usage WHERE day=?", (day,))
        row = await cur.fetchone()
        return int(row["tokens"]) if row else 0
