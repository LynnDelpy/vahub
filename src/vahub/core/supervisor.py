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
    stderr_ring: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=STDERR_RING)
    )
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

    # --- discovery ---------------------------------------------------------
    def venv_path(self, name: str) -> Path:
        return self._state_dir / "modules" / name / "venv"

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
            try:
                manifest = Manifest.from_file(path)
                expanded = manifest.expand(
                    venv=self.venv_path(manifest.name),
                    state=self._state_dir,
                    config=self._config_dir,
                )
            except Exception as e:
                log.error("manifest_invalid", path=str(path), error=str(e))
                continue
            mod = Module(manifest=expanded)
            mod.missing_config = [
                k
                for k in expanded.config.required
                if resolve_config_value(expanded.name, k, os.environ) is None
            ]
            if mod.missing_config:
                mod.state = State.UNCONFIGURED
                mod.last_error = f"missing config: {', '.join(mod.missing_config)}"
            else:
                mod.state = State.STOPPED
            self.modules[expanded.name] = mod
            metrics.set_module_state(expanded.name, mod.state.value)
            log.info("module_discovered", module=expanded.name, state=mod.state.value)

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
            log.warning(
                "module_restart_backoff", module=mod.name, attempt=mod.restarts, delay_s=delay
            )
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
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except TimeoutError:
            pass
        # The module ignored SIGTERM. It has its own process group, so kill the
        # group: a module that forked helpers should not leave them behind.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        await proc.wait()

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
