"""Module lifecycle: discover, spawn, handshake, probe, restart.

    unconfigured -> starting -> ready
                        |         |
                        |         v
                        |     degraded -> ready
                        v         |
                     failed <-----+
                        |
                        v
                     stopped

`degraded` is deliberately not `failed`. A module whose backend is temporarily
unreachable (the Home Assistant box is rebooting) is still a healthy process,
and restarting it would neither help nor stop once it started.

A module is spawned with a minimal environment: only the variables its manifest
declares, never the hub's own. That is what keeps one module's token out of
another module's process, and it is the reason a missing declaration shows up as
`unconfigured` rather than as a mysterious runtime failure.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import os
import signal
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..contracts.manifest import HEALTH_TOOL, Manifest, module_env_prefix, resolve_config_value
from . import metrics
from .bus import EventBus
from .logging import get_logger
from .mcpclient import LINE_LIMIT, McpClient, McpError

log = get_logger("supervisor")

# Enough lines to explain why a module died, small enough to keep per module.
STDERR_RING = 200


# Cap on the backoff exponent: 2**6 is about a minute, which is long enough for
# a transient failure to clear and short enough that a recovered module comes
# back without an operator.
_MAX_BACKOFF_EXP = 6


class State(StrEnum):
    UNCONFIGURED = "unconfigured"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class Module:
    manifest: Manifest  # placeholders already expanded
    state: State = State.UNCONFIGURED
    proc: asyncio.subprocess.Process | None = None
    client: McpClient | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    last_error: str | None = None
    health: dict[str, Any] = field(default_factory=dict)
    restarts: int = 0
    ready_since: float | None = None
    missing_config: list[str] = field(default_factory=list)
    stderr_ring: collections.deque[str] = field(default_factory=lambda: collections.deque(maxlen=STDERR_RING))
    # One in-flight call per module: health probes and tool calls share it, so a
    # probe can never interleave with a real call on the same pipe.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def name(self) -> str:
        return self.manifest.name

    def has_health_tool(self) -> bool:
        return any(t.get("name") == HEALTH_TOOL for t in self.tools)


class Supervisor:
    def __init__(
        self,
        bus: EventBus,
        modules_dir: Path,
        state_dir: Path,
        config_dir: Path,
    ) -> None:
        self._bus = bus
        self._modules_dir = Path(modules_dir)
        self._state_dir = Path(state_dir)
        self._config_dir = Path(config_dir)
        self.modules: dict[str, Module] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._side_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        # Serialises the runtime lifecycle operations (load/apply_config/stop/
        # remove). The web layer can issue them concurrently for one module (a
        # config change is not under the install lock), and without this a remove
        # racing a config change could leave a supervise task untracked, leaking a
        # child process the shutdown path never kills.
        self._lifecycle = asyncio.Lock()
        # A snapshot of module configuration set through the web UI (and stored in
        # the database), consulted when the host environment does not already
        # provide a declared key. Kept here so _child_env and discover stay
        # synchronous; the runtime loads it before discovery and the web layer
        # keeps it current.
        self._db_config: dict[str, dict[str, str]] = {}

    # --- discovery ---------------------------------------------------------
    def venv_path(self, name: str) -> Path:
        return self._state_dir / "modules" / name / "venv"

    def _manifest_path(self, name: str) -> Path:
        return self._modules_dir / f"{name}.yaml"

    def set_db_config(self, config: dict[str, dict[str, str]]) -> None:
        """Replace the whole snapshot of UI-provided module configuration."""
        self._db_config = {name: dict(values) for name, values in config.items()}

    def update_module_config(self, name: str, config: dict[str, str]) -> None:
        """Update one module's stored configuration in the snapshot. Call
        apply_config() afterwards to (re)start or stop the module accordingly."""
        if config:
            self._db_config[name] = dict(config)
        else:
            self._db_config.pop(name, None)

    def discover(self) -> None:
        """Read every manifest without spawning anything, so the dashboard can
        list a module that is installed but not yet configured.

        The directory is walked here rather than through contracts.load_manifests
        because one unreadable manifest must not stop every other module from
        starting."""
        if not self._modules_dir.is_dir():
            log.warning("modules_dir_missing", path=str(self._modules_dir))
            return
        for path in sorted(self._modules_dir.glob("*.yaml")):
            mod = self._build_module(path)
            if mod is None:
                continue
            mod.state = State.UNCONFIGURED if mod.missing_config else State.STOPPED
            self.modules[mod.name] = mod
            metrics.set_module_state(mod.name, mod.state.value)
            log.info("module_discovered", module=mod.name, state=mod.state.value)

    def _build_module(self, path: Path) -> Module | None:
        """Read and expand one manifest into a Module with its config status, or
        None if the manifest cannot be read or validated (which must not stop the
        other modules)."""
        try:
            manifest = Manifest.from_file(path)
            expanded = manifest.expand(
                venv=self.venv_path(manifest.name),
                state=self._state_dir,
                config=self._config_dir,
            )
        except Exception as e:
            log.error("manifest_invalid", path=str(path), error=str(e))
            return None
        mod = Module(manifest=expanded)
        self._refresh_config(mod)
        return mod

    def _refresh_config(self, mod: Module) -> None:
        """Recompute which required keys a module is still missing, from the host
        environment and the stored UI configuration together."""
        stored = self._db_config.get(mod.name, {})
        mod.missing_config = [
            k
            for k in mod.manifest.config.required
            if resolve_config_value(mod.name, k, os.environ, stored) is None
        ]
        mod.last_error = (
            f"missing config: {', '.join(mod.missing_config)}" if mod.missing_config else mod.last_error
        )

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        for name, mod in self.modules.items():
            if mod.state is State.UNCONFIGURED or name in self._tasks:
                continue
            self._tasks[name] = asyncio.create_task(self._supervise(mod), name=f"supervise:{name}")

    async def restart(self, name: str) -> bool:
        """Force a restart. The supervise loop notices the process is gone and
        starts it again, so the retry budget still applies."""
        mod = self.modules.get(name)
        if mod is None or mod.state is State.UNCONFIGURED:
            return False
        mod.restarts = 0  # an operator asking for this is not a crash loop
        if mod.proc is not None and mod.proc.returncode is None:
            await self._kill(mod.proc)
            return True
        if name not in self._tasks or self._tasks[name].done():
            self._tasks[name] = asyncio.create_task(self._supervise(mod), name=f"supervise:{name}")
        return True

    def _running(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()

    # The four runtime lifecycle operations the web layer drives. Each takes the
    # lifecycle lock and delegates to an unlocked `_`-internal, so they never
    # interleave (the internals call each other directly, already holding it).
    async def load_module(self, name: str) -> bool:
        async with self._lifecycle:
            return await self._load_module(name)

    async def apply_config(self, name: str) -> bool:
        async with self._lifecycle:
            return await self._apply_config(name)

    async def stop_module(self, name: str) -> bool:
        async with self._lifecycle:
            return await self._stop_module(name)

    async def remove_module(self, name: str) -> bool:
        async with self._lifecycle:
            return await self._remove_module(name)

    async def _load_module(self, name: str) -> bool:
        """Bring a newly installed (or reinstalled) module into a running hub
        without a hub restart.

        The manifest is always read fresh from disk, so a reinstall picks up a
        changed command, tool set or config; if the module was already running it
        is stopped first and replaced by the fresh instance. A module that still
        needs a token is added in the unconfigured state and will start the moment
        apply_config() sees the token arrive."""
        path = self._manifest_path(name)
        if not path.is_file():
            return False
        mod = self._build_module(path)
        if mod is None:
            return False
        # Stop the previous instance (if any) before swapping in the fresh one, so
        # a reinstall does not leave the old process running against the old code.
        if self._running(name):
            await self._stop_module(name)
        self.modules[mod.name] = mod
        if mod.missing_config or self._stopping:
            self._set_state(mod, State.UNCONFIGURED if mod.missing_config else State.STOPPED)
            return True
        self._set_state(mod, State.STARTING)
        self._tasks[mod.name] = asyncio.create_task(self._supervise(mod), name=f"supervise:{mod.name}")
        return True

    async def _apply_config(self, name: str) -> bool:
        """React to a change in a module's stored configuration: start it if it
        is now ready, restart it if it is running so it picks up the new values,
        or stop it if it lost a required key."""
        mod = self.modules.get(name)
        if mod is None:
            return await self._load_module(name)
        self._refresh_config(mod)
        if mod.missing_config:
            if self._running(name):
                await self._stop_module(name)
            self._set_state(mod, State.UNCONFIGURED)
            return True
        if self._running(name):
            return await self.restart(name)  # bounce it: new environment on respawn
        if self._stopping:
            return False
        mod.restarts = 0
        mod.last_error = None
        self._set_state(mod, State.STARTING)
        self._tasks[name] = asyncio.create_task(self._supervise(mod), name=f"supervise:{name}")
        return True

    async def _stop_module(self, name: str) -> bool:
        """Stop one module: cancel its supervise loop, kill the process, close the
        client. The module row stays so the UI still lists it as stopped; use
        remove_module to drop it entirely."""
        mod = self.modules.get(name)
        if mod is None:
            return False
        task = self._tasks.pop(name, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if mod.proc is not None and mod.proc.returncode is None:
            await self._kill(mod.proc)
        if mod.client is not None:
            await mod.client.close()
            mod.client = None
        if mod.state not in (State.UNCONFIGURED, State.FAILED):
            self._set_state(mod, State.STOPPED)
        return True

    async def _remove_module(self, name: str) -> bool:
        """Stop a module and forget it entirely. The caller deletes the manifest
        and the venv on disk; this only detaches it from the running hub."""
        if name not in self.modules:
            return False
        await self._stop_module(name)
        self.modules.pop(name, None)
        # Cancel any task still tracked before dropping the reference, so a
        # supervise loop can never outlive the module as an untracked orphan
        # (which stop() at shutdown would then never reach).
        task = self._tasks.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._db_config.pop(name, None)
        return True

    async def _supervise(self, mod: Module) -> None:
        restart = mod.manifest.restart
        while not self._stopping:
            if await self._spawn_and_init(mod):
                await self._run_until_exit(mod)
                if self._stopping:
                    return
            if not self._register_failure(mod):
                return  # retry budget spent, state is already FAILED
            delay = restart.backoff_base_s ** min(mod.restarts, _MAX_BACKOFF_EXP)
            log.warning("module_restart_backoff", module=mod.name, attempt=mod.restarts, delay_s=delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

    async def _run_until_exit(self, mod: Module) -> None:
        proc = mod.proc
        if proc is None:  # pragma: no cover - defensive
            return
        health_task = asyncio.create_task(self._health_loop(mod), name=f"health:{mod.name}")
        try:
            exit_code = await proc.wait()
        finally:
            health_task.cancel()
            # A failing health probe must never take the supervise loop down, or
            # the module would be stuck: never restarted and never stopped.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await health_task
            if mod.client is not None:
                await mod.client.close()
                mod.client = None
        if self._stopping:
            return
        mod.last_error = f"process exited (code {exit_code})"
        log.warning("module_exited", module=mod.name, code=exit_code)

    def _register_failure(self, mod: Module) -> bool:
        """Count a failure, forgiving earlier ones if the module had been up long
        enough. Returns False when the retry budget is spent."""
        restart = mod.manifest.restart
        if mod.ready_since is not None and (time.monotonic() - mod.ready_since) >= restart.reset_after_s:
            mod.restarts = 0
        mod.ready_since = None
        mod.restarts += 1
        metrics.MODULE_RESTARTS.labels(module=mod.name).inc()
        if mod.restarts > restart.max_retries:
            self._set_state(mod, State.FAILED)
            log.error("module_failed_permanently", module=mod.name, restarts=mod.restarts)
            return False
        self._set_state(mod, State.STARTING)
        return True

    async def _spawn_and_init(self, mod: Module) -> bool:
        manifest = mod.manifest
        self._set_state(mod, State.STARTING)
        try:
            if manifest.runtime.cwd:
                Path(manifest.runtime.cwd).mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *manifest.runtime.command,  # argv list, never a shell string
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(manifest),
                cwd=manifest.runtime.cwd,
                preexec_fn=self._preexec(manifest),
                # Own process group: Ctrl-C in a terminal reaches the hub only,
                # which then shuts modules down in order instead of racing them.
                start_new_session=True,
                limit=LINE_LIMIT,
            )
        except Exception as e:
            mod.last_error = f"spawn failed: {e}"
            log.error("module_spawn_failed", module=manifest.name, error=str(e))
            return False

        mod.proc = proc
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:  # pragma: no cover
            mod.last_error = "spawn produced no pipes"
            await self._kill(proc)
            return False

        self._spawn_side_task(self._drain_stderr(mod, proc.stderr), f"stderr:{manifest.name}")
        client = McpClient(manifest.name, proc.stdin, proc.stdout, get_logger(f"mcp.{manifest.name}"))
        client.start()
        mod.client = client

        # The handshake is bounded, otherwise a module that starts but never
        # answers leaves the hub in `starting` for good.
        timeout = manifest.restart.startup_timeout_s
        try:
            async with asyncio.timeout(timeout):
                await client.initialize(timeout)
                mod.tools = await client.list_tools(timeout)
        except asyncio.CancelledError:
            await client.close()
            await self._kill(proc)
            raise
        except Exception as e:
            # A timeout stringifies to nothing, and "handshake failed:" with an
            # empty reason is the least useful line in an incident.
            reason = f"no response within {timeout}s" if isinstance(e, TimeoutError) else str(e)
            mod.last_error = f"handshake failed: {reason}"
            log.error("module_handshake_failed", module=manifest.name, error=reason)
            await client.close()
            mod.client = None
            await self._kill(proc)
            return False

        mod.ready_since = time.monotonic()
        mod.last_error = None
        self._set_state(mod, State.READY)
        log.info(
            "module_ready",
            module=manifest.name,
            server=client.server_info,
            tools=[t.get("name") for t in mod.tools],
        )
        if not mod.has_health_tool():
            # Without the reserved probe the hub can only observe the process,
            # not whatever it talks to. Say so once instead of parking the module
            # in `degraded` forever for a call it does not implement.
            log.warning("module_without_health_tool", module=manifest.name)
        return True

    async def _health_loop(self, mod: Module) -> None:
        health = mod.manifest.health
        while True:
            await asyncio.sleep(health.interval_s)
            client = mod.client
            if client is None or not mod.has_health_tool():
                continue
            try:
                async with mod.lock:  # never concurrent with a real tool call
                    raw = await client.call_tool(HEALTH_TOOL, {}, health.timeout_s)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._unhealthy(mod, "health timeout")
                continue
            except McpError as e:
                self._unhealthy(mod, e.message)
                continue
            except Exception as e:
                self._unhealthy(mod, f"health error: {e}")
                continue

            if not isinstance(raw, dict):
                # Module output is untrusted: a non-object result is a sick
                # module, not a reason to lose the probe loop.
                self._unhealthy(mod, "malformed health result")
                continue
            payload = extract_payload(raw)
            mod.health = payload if isinstance(payload, dict) else {"raw": payload}
            ok = raw.get("isError") is not True and (
                not isinstance(payload, dict) or payload.get("ok", True) is not False
            )
            if ok:
                mod.last_error = None
            self._set_state(mod, State.READY if ok else State.DEGRADED)

    def _unhealthy(self, mod: Module, detail: str) -> None:
        mod.health = {"ok": False, "detail": detail}
        mod.last_error = detail
        self._set_state(mod, State.DEGRADED)

    # --- process environment ----------------------------------------------
    def _child_env(self, manifest: Manifest) -> dict[str, str]:
        """The variables the manifest declares, plus the few a process needs to
        run at all. No blanket os.environ passthrough: the clock module has no
        business reading a Home Assistant token.

        A declared key is looked up first under a per-module name,
        ``VAHUB_MOD_<NAME>_<KEY>``, and only then under the bare ``<KEY>``. The
        scoped form is what keeps one module's secret out of another's reach: a
        hostile manifest that lists ``HA_TOKEN`` gets ``VAHUB_MOD_<ITSELF>_HA_TOKEN``,
        which the operator never set, not the value meant for the Home Assistant
        module. The bare fallback stays for existing deployments, but it is a
        shared value any module can name, so it is logged when used.
        """
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            # Buffered stdout in a child would stall the line-delimited framing
            # until the buffer happens to fill.
            "PYTHONUNBUFFERED": "1",
        }
        scope = module_env_prefix(manifest.name)
        stored = self._db_config.get(manifest.name, {})
        for key in (*manifest.config.required, *manifest.config.optional):
            scoped = os.environ.get(scope + key)
            if scoped is not None:
                env[key] = scoped
                continue
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
                log.warning(
                    "module_env_unscoped",
                    module=manifest.name,
                    key=key,
                    detail=(
                        f"{key} is read from the shared hub environment, so any installed "
                        f"module that declares {key} receives it. Set {scope}{key} instead to "
                        f"scope this secret to the {manifest.name!r} module."
                    ),
                )
                continue
            # Set in the web UI and stored in the database. This is already
            # scoped to one module, so it is used without the shared-secret
            # warning above.
            stored_value = stored.get(key)
            if stored_value is not None:
                env[key] = stored_value
        if manifest.runtime.pythonpath:  # only for modules installed from a checkout
            env["PYTHONPATH"] = manifest.runtime.pythonpath
        if manifest.runtime.user:
            home = _home_of(manifest.runtime.user)
            if home:
                env["HOME"] = home
        return env

    def _preexec(self, manifest: Manifest) -> Callable[[], None] | None:
        """Drop to the module's own uid before exec. Only possible when the hub
        runs as root; unprivileged deployments (containers) simply keep the
        hub's uid, which is why this is best effort and not an error."""
        user = manifest.runtime.user
        if not user or os.geteuid() != 0:
            return None
        try:
            import pwd

            pw = pwd.getpwnam(user)
        except KeyError:
            log.warning("module_user_unknown", module=manifest.name, user=user)
            return None

        def _drop() -> None:
            # initgroups first: without it the child keeps root's supplementary
            # groups (group 0, device groups) after setuid, and the drop is
            # worth much less than it looks.
            os.initgroups(user, pw.pw_gid)
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)

        return _drop

    async def _drain_stderr(self, mod: Module, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                try:
                    line = await stream.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    continue  # one absurdly long log line, keep reading
                if not line:
                    break
                text = line.rstrip().decode("utf-8", "replace")
                mod.stderr_ring.append(text)
                self._bus.publish("module.log", {"module": mod.name, "line": text})
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            pass

    def _spawn_side_task(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    # --- state -------------------------------------------------------------
    def _set_state(self, mod: Module, new: State) -> None:
        if mod.state is new:
            return
        old = mod.state
        mod.state = new
        metrics.set_module_state(mod.name, new.value)
        self._bus.publish(
            "module.state_changed",
            {
                "module": mod.name,
                "state": new.value,
                "previous": old.value,
                "last_error": mod.last_error,
            },
        )
        log.info("module_state_changed", module=mod.name, previous=old.value, state=new.value)

    def snapshot(self) -> list[dict[str, Any]]:
        """A serialisable view for the CLI (`vahub doctor`) and the state mirror.
        Everything here except the module's own name comes from the hub, not from
        the module, with the exception of health/stderr, which callers must treat
        as text."""
        out: list[dict[str, Any]] = []
        for mod in sorted(self.modules.values(), key=lambda m: m.name):
            out.append(
                {
                    "name": mod.name,
                    "version": mod.manifest.version,
                    "description": mod.manifest.description,
                    "state": mod.state.value,
                    "last_error": mod.last_error,
                    "restarts": mod.restarts,
                    "pid": mod.proc.pid if mod.proc and mod.proc.returncode is None else None,
                    "tools": [t.get("name") for t in mod.tools if t.get("name") != HEALTH_TOOL],
                    "health": mod.health,
                    "missing_config": list(mod.missing_config),
                    "stderr_tail": list(mod.stderr_ring)[-20:],
                }
            )
        return out

    # --- shutdown ----------------------------------------------------------
    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        # Signal the whole process group, not just the leader, on the graceful
        # path too: the module is spawned with start_new_session=True, so a helper
        # it forked lives in its group. terminate()/kill() would reach only the
        # leader, orphaning the helper if the leader honours SIGTERM and exits.
        self._signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except TimeoutError:
            pass
        # It ignored SIGTERM. Take the group down hard so nothing is left behind.
        self._signal_group(proc, signal.SIGKILL)
        await proc.wait()

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        """Send `sig` to the child's whole process group, falling back to the lone
        process if the group is already gone. While the leader is alive its pgid
        equals its pid (start_new_session), so this reaches its forked helpers."""
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(sig)

    async def stop(self) -> None:
        self._stopping = True
        for mod in self.modules.values():
            if mod.proc is not None and mod.proc.returncode is None:
                await self._kill(mod.proc)
            if mod.client is not None:
                await mod.client.close()
                mod.client = None
        for task in (*self._tasks.values(), *self._side_tasks):
            task.cancel()
        for task in (*self._tasks.values(), *self._side_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self._side_tasks.clear()
        for mod in self.modules.values():
            if mod.state not in (State.UNCONFIGURED, State.FAILED):
                self._set_state(mod, State.STOPPED)


def _home_of(user: str) -> str | None:
    try:
        import pwd

        return pwd.getpwnam(user).pw_dir
    except (KeyError, ImportError):
        return None


def extract_payload(raw: Any) -> Any:
    """Pull a usable payload out of an MCP tools/call result.

    The result is untrusted, so every level is checked before it is indexed. A
    text block is reinterpreted as JSON only when it clearly is an object or an
    array, so a tool whose real answer is the string "123" keeps its type."""
    if not isinstance(raw, dict):
        return raw
    if raw.get("structuredContent") is not None:
        return raw["structuredContent"]
    content = raw.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    joined = "\n".join(parts)
    stripped = joined.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except ValueError:
            return joined
    return joined
