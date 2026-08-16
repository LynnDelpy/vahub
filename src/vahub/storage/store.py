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

import contextlib
import json
import os
import time
from pathlib import Path
from types import TracebackType
from typing import Any

import aiosqlite


def _chmod_quiet(path: Path, mode: int) -> None:
    """Best effort chmod. A missing file (a WAL sidecar not yet created) or a
    filesystem that does not carry Unix modes must not stop the hub starting."""
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


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

# v2: accounts, sessions, and the runtime-editable state the UI and the AI own:
# preferences, saved locations, and schedules created at runtime. Policy rules
# and accounts are deliberately NOT in here as UI-editable rows; policy stays in
# the file, and accounts are managed only by the CLI.
_V2: tuple[str, ...] = (
    """
    CREATE TABLE users (
        username      TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,   -- scrypt$..., never a clear password
        display_name  TEXT,
        created_at    REAL NOT NULL,
        disabled      INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE sessions (
        id          TEXT PRIMARY KEY,  -- opaque random token, the cookie value
        username    TEXT NOT NULL,
        created_at  REAL NOT NULL,
        expires_at  REAL NOT NULL
    )
    """,
    # Preferences: units, spoken language, TTS voice, and anything else a person
    # or the assistant wants to remember. Value is JSON so it is not limited to
    # strings.
    """
    CREATE TABLE app_settings (
        key         TEXT PRIMARY KEY,
        value       TEXT,              -- JSON-encoded
        updated_at  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE locations (
        name        TEXT PRIMARY KEY,  -- home, work, gym, ...
        label       TEXT,
        latitude    REAL,
        longitude   REAL,
        address     TEXT,
        updated_at  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE dyn_schedules (
        id          TEXT PRIMARY KEY,
        cron        TEXT NOT NULL,
        enabled     INTEGER NOT NULL DEFAULT 1,
        steps       TEXT NOT NULL,     -- JSON list of {module, tool, args, timeout_s}
        description TEXT,
        created_by  TEXT,              -- the username or the assistant
        created_at  REAL NOT NULL
    )
    """,
    "CREATE INDEX idx_sessions_expires ON sessions(expires_at)",
)

MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = ((1, _V1), (2, _V2))

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
        # The database holds conversations, tool arguments (which can carry
        # secrets) and the audit log. None of it should be readable by other
        # users on the host, so the directory is private before the file is
        # created and the file itself is tightened once SQLite has made it.
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chmod_quiet(self._path.parent, 0o700)
        db = await aiosqlite.connect(self._path, isolation_level=None)
        db.row_factory = aiosqlite.Row
        # WAL lets a reader (`vahub audit`, a backup) run while a writer works.
        await db.execute("PRAGMA journal_mode=WAL")
        # 0600 on the database and its WAL/SHM sidecars. WAL mode creates the
        # sidecars lazily, so re-tighten them after the first write below as well.
        for suffix in ("", "-wal", "-shm"):
            _chmod_quiet(Path(str(self._path) + suffix), 0o600)
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        # Rather than surfacing "database is locked" the moment a VACUUM INTO
        # backup overlaps a write.
        await db.execute("PRAGMA busy_timeout=5000")
        self._db = db
        try:
            await self._migrate()
        except Exception:
            # A half-applied migration must not leave the store looking open with
            # an incomplete schema. Drop the connection so open() can be retried
            # or the failure surfaced, rather than every later query failing oddly.
            await db.close()
            self._db = None
            raise
        # The migration wrote, so the WAL/SHM sidecars now exist. Tighten them
        # once more; the earlier pass ran before they were created.
        for suffix in ("-wal", "-shm"):
            _chmod_quiet(Path(str(self._path) + suffix), 0o600)

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

    async def recent_tool_calls(
        self, limit: int = 100, *, principal: str | None = None, decision: str | None = None
    ) -> list[dict[str, Any]]:
        # Filters are applied in SQL, before the limit, so `--denied -n 50` means
        # the last 50 denied calls, not the denials that happen to fall within the
        # last 50 calls of any kind.
        where: list[str] = []
        params: list[Any] = []
        if principal:
            where.append("principal = ?")
            params.append(principal)
        if decision:
            where.append("decision = ?")
            params.append(decision)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(max(1, int(limit)))
        cur = await self.db.execute(
            "SELECT id, ts, principal, module, tool, args, decision, result, duration_ms"
            f" FROM tool_calls{clause} ORDER BY id DESC LIMIT ?",
            tuple(params),
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

    async def consume_pending(self, pid: str) -> bool:
        """Atomically move one pending call to 'confirmed'. Returns True only for
        the caller that actually made the transition.

        A confirmation must fire the frozen call exactly once. Two requests for
        the same id would otherwise both read 'pending', both dispatch, and a
        single human approval would unlock the door twice. The guard is the
        `AND status='pending'` clause: only one UPDATE changes a row, so only one
        caller sees rowcount == 1.
        """
        cur = await self.db.execute(
            "UPDATE pending_calls SET status='confirmed' WHERE id=? AND status='pending'",
            (pid,),
        )
        return bool(cur.rowcount and cur.rowcount == 1)

    async def list_pending(self) -> list[dict[str, Any]]:
        """Confirmations that can still be acted on, including their frozen
        arguments so the approval card can show what will actually run (the same
        values the live confirmation event carries). This feeds the assistant
        page only, which is already origin-checked because these ids confirm a
        destructive action."""
        cur = await self.db.execute(
            "SELECT id, ts, expires_at, principal, module, tool, status, args FROM pending_calls"
            " WHERE status='pending' AND expires_at > ? ORDER BY ts DESC",
            (time.time(),),
        )
        rows: list[dict[str, Any]] = []
        for row in await cur.fetchall():
            item = dict(row)
            try:
                item["args"] = json.loads(item.get("args") or "{}")
            except ValueError:
                item["args"] = {}
            rows.append(item)
        return rows

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

    # --- accounts ---------------------------------------------------------
    async def create_user(self, username: str, password_hash: str, display_name: str | None) -> None:
        await self.db.execute(
            "INSERT INTO users(username, password_hash, display_name, created_at, disabled)"
            " VALUES(?,?,?,?,0)",
            (username, password_hash, display_name, time.time()),
        )

    async def get_user(self, username: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM users WHERE username=?", (username,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_users(self) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT username, display_name, created_at, disabled FROM users ORDER BY username"
        )
        return [dict(row) for row in await cur.fetchall()]

    async def count_users(self) -> int:
        """Total accounts, disabled included. "Setup required" means there is no
        account at all; a disabled account is not the same as none, so it must
        not flip the hub back into first-run state."""
        cur = await self.db.execute("SELECT COUNT(*) AS n FROM users")
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def set_password(self, username: str, password_hash: str) -> bool:
        cur = await self.db.execute(
            "UPDATE users SET password_hash=? WHERE username=?", (password_hash, username)
        )
        return bool(cur.rowcount)

    async def set_user_disabled(self, username: str, disabled: bool) -> bool:
        cur = await self.db.execute(
            "UPDATE users SET disabled=? WHERE username=?", (1 if disabled else 0, username)
        )
        return bool(cur.rowcount)

    async def delete_user(self, username: str) -> bool:
        await self.db.execute("DELETE FROM sessions WHERE username=?", (username,))
        cur = await self.db.execute("DELETE FROM users WHERE username=?", (username,))
        return bool(cur.rowcount)

    # --- sessions ---------------------------------------------------------
    async def create_session(self, sid: str, username: str, expires_at: float) -> None:
        await self.db.execute(
            "INSERT INTO sessions(id, username, created_at, expires_at) VALUES(?,?,?,?)",
            (sid, username, time.time(), expires_at),
        )

    async def session_user(self, sid: str) -> str | None:
        """The username for a live, unexpired session, or None. A disabled
        account's sessions never resolve, so revoking access is immediate."""
        cur = await self.db.execute(
            "SELECT s.username FROM sessions s JOIN users u ON u.username = s.username"
            " WHERE s.id=? AND s.expires_at > ? AND u.disabled=0",
            (sid, time.time()),
        )
        row = await cur.fetchone()
        return str(row["username"]) if row else None

    async def delete_session(self, sid: str) -> None:
        await self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))

    async def drop_user_sessions(self, username: str) -> None:
        """End every session for an account, e.g. after a password change."""
        await self.db.execute("DELETE FROM sessions WHERE username=?", (username,))

    async def sweep_sessions(self) -> int:
        cur = await self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # --- preferences (app_settings) --------------------------------------
    async def get_setting(self, key: str) -> Any:
        cur = await self.db.execute("SELECT value FROM app_settings WHERE key=?", (key,))
        row = await cur.fetchone()
        if row is None or row["value"] is None:
            return None
        try:
            return json.loads(row["value"])
        except ValueError:
            return None

    async def all_settings(self) -> dict[str, Any]:
        cur = await self.db.execute("SELECT key, value FROM app_settings ORDER BY key")
        out: dict[str, Any] = {}
        for row in await cur.fetchall():
            try:
                out[row["key"]] = json.loads(row["value"]) if row["value"] is not None else None
            except ValueError:
                out[row["key"]] = None
        return out

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), time.time()),
        )

    async def delete_setting(self, key: str) -> bool:
        cur = await self.db.execute("DELETE FROM app_settings WHERE key=?", (key,))
        return bool(cur.rowcount)

    # --- locations --------------------------------------------------------
    async def list_locations(self) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM locations ORDER BY name")
        return [dict(row) for row in await cur.fetchall()]

    async def get_location(self, name: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM locations WHERE name=?", (name,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_location(
        self,
        name: str,
        *,
        label: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        address: str | None = None,
    ) -> None:
        # COALESCE so a partial update (e.g. renaming the label of a place that
        # already has coordinates) keeps the fields it did not mention, instead
        # of clobbering them to NULL.
        await self.db.execute(
            "INSERT INTO locations(name, label, latitude, longitude, address, updated_at)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET"
            " label=COALESCE(excluded.label, locations.label),"
            " latitude=COALESCE(excluded.latitude, locations.latitude),"
            " longitude=COALESCE(excluded.longitude, locations.longitude),"
            " address=COALESCE(excluded.address, locations.address),"
            " updated_at=excluded.updated_at",
            (name, label, latitude, longitude, address, time.time()),
        )

    async def delete_location(self, name: str) -> bool:
        cur = await self.db.execute("DELETE FROM locations WHERE name=?", (name,))
        return bool(cur.rowcount)

    # --- runtime schedules ------------------------------------------------
    async def list_dyn_schedules(self) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM dyn_schedules ORDER BY created_at DESC")
        out: list[dict[str, Any]] = []
        for row in await cur.fetchall():
            item = dict(row)
            item["enabled"] = bool(item.get("enabled"))
            try:
                item["steps"] = json.loads(item.get("steps") or "[]")
            except ValueError:
                item["steps"] = []
            out.append(item)
        return out

    async def add_dyn_schedule(
        self,
        sid: str,
        cron: str,
        steps: list[dict[str, Any]],
        *,
        description: str | None = None,
        created_by: str | None = None,
        enabled: bool = True,
    ) -> None:
        await self.db.execute(
            "INSERT INTO dyn_schedules(id, cron, enabled, steps, description, created_by, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (sid, cron, 1 if enabled else 0, json.dumps(steps), description, created_by, time.time()),
        )

    async def set_dyn_schedule_enabled(self, sid: str, enabled: bool) -> bool:
        cur = await self.db.execute(
            "UPDATE dyn_schedules SET enabled=? WHERE id=?", (1 if enabled else 0, sid)
        )
        return bool(cur.rowcount)

    async def delete_dyn_schedule(self, sid: str) -> bool:
        cur = await self.db.execute("DELETE FROM dyn_schedules WHERE id=?", (sid,))
        return bool(cur.rowcount)
