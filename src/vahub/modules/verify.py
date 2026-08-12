"""The module contract test kit: does this program behave like a vahub module?

`vahub module verify` and a module author's CI run exactly this code, and it
spawns the module the way the hub does, with the same minimal environment and
the same MCP client. Verifying against a second, friendlier client would let a
module pass here and still fail in the hub, which is the one outcome the kit
exists to prevent.

Every check reports its own verdict. "It failed" is not useful to someone
writing a module; "the handshake succeeded, __health returned a string" is.

Two things verify deliberately does not do: it does not drop privileges to
`runtime.user` (it is a test, run by a person, not the hub), and it does not
judge whether a backend is reachable. A module reporting ok=false is healthy
behaviour from a module whose backend is down, and it is reported as a warning.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from vahub.contracts.manifest import HEALTH_TOOL, TOOL_RE, Manifest
from vahub.modules.store import ModuleStore

if TYPE_CHECKING:
    from vahub.config.models import Config

Severity = Literal["error", "warning"]

# Fields the documented health payload carries. Only `ok` is required; the rest
# make a status page useful and their absence is worth mentioning, not failing.
HEALTH_FIELDS = ("backend", "latency_ms", "detail")

DEFAULT_STARTUP_TIMEOUT_S = 20.0
DEFAULT_HEALTH_TIMEOUT_S = 10.0
STDERR_KEPT_LINES = 50


class VerifyError(Exception):
    """Raised when verification cannot even be attempted (no manifest, say)."""


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    severity: Severity = "error"

    @property
    def label(self) -> str:
        if self.ok:
            return "pass"
        return "FAIL" if self.severity == "error" else "warn"


@dataclass
class VerifyReport:
    module: str
    source: str = ""
    checks: list[Check] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", severity: Severity = "error") -> Check:
        check = Check(name=name, ok=ok, detail=detail, severity=severity)
        self.checks.append(check)
        return check

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.failures

    def text(self) -> str:
        width = max((len(c.name) for c in self.checks), default=0)
        lines = [f"{c.label:>4}  {c.name:<{width}}  {c.detail}".rstrip() for c in self.checks]
        summary = "ok" if self.ok else f"{len(self.failures)} failed"
        lines.append(f"      {len(self.checks)} checks, {len(self.warnings)} warnings, {summary}")
        if not self.ok and self.stderr:
            lines.append("      module stderr:")
            lines.extend(f"        {line}" for line in self.stderr[-20:])
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "source": self.source,
            "ok": self.ok,
            "tools": self.tools,
            "checks": [
                {"name": c.name, "ok": c.ok, "severity": c.severity, "detail": c.detail}
                for c in self.checks
            ],
            "stderr": self.stderr,
        }


class _Recorder:
    """The hub's MCP client logs through a structlog-style logger. Verify wants
    those events as data: a module printing to stdout breaks the framing, and
    that shows up here as a parse warning rather than as an exception."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def _record(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    debug = info = warning = error = _record

    def count(self, *names: str) -> int:
        return sum(1 for event, _ in self.events if event in names)


async def verify_manifest(
    manifest: Manifest,
    *,
    venv: Path,
    state: Path,
    config_dir: Path,
    env: Mapping[str, str] | None = None,
    source: str = "",
    startup_timeout_s: float | None = None,
    health_timeout_s: float | None = None,
) -> VerifyReport:
    """Spawn a module described by `manifest` and check the contract."""
    report = VerifyReport(module=manifest.name, source=source)
    report.add("manifest", True, f"{manifest.name} {manifest.version}")

    environ = os.environ if env is None else env
    missing = [key for key in manifest.config.required if not environ.get(key)]
    report.add(
        "config",
        not missing,
        f"missing: {', '.join(missing)}" if missing else "required keys present",
        severity="warning",
    )

    expanded = manifest.expand(venv=venv, state=state, config=config_dir)
    command = list(expanded.runtime.command)
    executable = command[0]
    if executable.startswith("/") and not Path(executable).exists():
        report.add("spawn", False, f"{executable} does not exist")
        return report

    startup = startup_timeout_s if startup_timeout_s is not None else manifest.restart.startup_timeout_s
    health_timeout = health_timeout_s if health_timeout_s is not None else DEFAULT_HEALTH_TIMEOUT_S
    cwd = expanded.runtime.cwd
    if cwd:
        with contextlib.suppress(OSError):
            Path(cwd).mkdir(parents=True, exist_ok=True)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(expanded, environ),
            cwd=cwd if cwd and Path(cwd).is_dir() else None,
            limit=1024 * 1024,
        )
    except OSError as e:
        report.add("spawn", False, f"{' '.join(command)}: {e}")
        return report
    report.add("spawn", True, " ".join(command))

    stderr_lines: collections.deque[str] = collections.deque(maxlen=STDERR_KEPT_LINES)
    drain = asyncio.create_task(_drain(process.stderr, stderr_lines))
    recorder = _Recorder()
    client = None
    try:
        client = _new_client(manifest.name, process, recorder)
        client.start()
        await _run_checks(report, client, manifest, startup, health_timeout)
        await _check_shutdown(report, process)
    finally:
        report.stderr = list(stderr_lines)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await drain
        await _kill(process)

    noise = recorder.count("mcp_bad_json", "mcp_non_object_message", "mcp_line_too_long")
    report.add(
        "stdout_clean",
        noise == 0,
        "stdout carries JSON-RPC only" if noise == 0 else f"{noise} unparseable line(s) on stdout",
        severity="warning",
    )
    return report


async def verify_installed(
    name: str,
    config: Config,
    *,
    store: ModuleStore | None = None,
    env: Mapping[str, str] | None = None,
    startup_timeout_s: float | None = None,
    health_timeout_s: float | None = None,
) -> VerifyReport:
    """Verify a module that `vahub module add` installed, as the hub would run it."""
    module_store = store if store is not None else ModuleStore.from_config(config)
    installed = module_store.get(name)
    if installed is None:
        raise VerifyError(f"module {name!r} is not installed")
    if installed.manifest is None:
        raise VerifyError(f"module {name!r} has no usable manifest: {installed.manifest_error}")
    return await verify_manifest(
        installed.manifest,
        venv=installed.venv,
        state=config.hub.state_dir,
        config_dir=config.hub.modules_dir.parent,
        env=env,
        source=str(installed.manifest_path),
        startup_timeout_s=startup_timeout_s,
        health_timeout_s=health_timeout_s,
    )


async def verify_source(
    path: Path,
    *,
    venv: Path | None = None,
    env: Mapping[str, str] | None = None,
    startup_timeout_s: float | None = None,
    health_timeout_s: float | None = None,
) -> VerifyReport:
    """Verify a module from its source directory, for use in its own CI.

    There is no installed venv there, so `{venv}` resolves to the environment
    the author is already running in: in CI that is the venv the module was just
    installed into, which is the thing under test."""
    manifest_path = path if path.is_file() else path / "module.yaml"
    if not manifest_path.is_file():
        raise VerifyError(f"no module.yaml at {manifest_path}")
    try:
        manifest = Manifest.from_file(manifest_path)
    except Exception as e:  # an invalid manifest is a check result, not a crash
        report = VerifyReport(module=manifest_path.parent.name, source=str(manifest_path))
        report.add("manifest", False, str(e))
        return report

    with tempfile.TemporaryDirectory(prefix="vahub-verify-") as tmp:
        return await verify_manifest(
            manifest,
            venv=venv or Path(sys.prefix),
            state=Path(tmp),
            config_dir=manifest_path.parent,
            env=env,
            source=str(manifest_path),
            startup_timeout_s=startup_timeout_s,
            health_timeout_s=health_timeout_s,
        )


def verify_installed_sync(name: str, config: Config, **kwargs: Any) -> VerifyReport:
    return asyncio.run(verify_installed(name, config, **kwargs))


def verify_source_sync(path: Path, **kwargs: Any) -> VerifyReport:
    return asyncio.run(verify_source(path, **kwargs))


# --------------------------------------------------------------------------
# the checks themselves
# --------------------------------------------------------------------------
async def _run_checks(
    report: VerifyReport,
    client: Any,
    manifest: Manifest,
    startup_s: float,
    health_timeout_s: float,
) -> None:
    try:
        await client.initialize(startup_s)
    except Exception as e:  # every failure here is the module's, and is reported as one
        report.add("handshake", False, f"initialize failed: {e}")
        return
    server = client.server_info if isinstance(client.server_info, dict) else {}
    report.add("handshake", True, f"{server.get('name', '?')} {server.get('version', '?')}")

    try:
        tools = await client.list_tools(startup_s)
    except Exception as e:
        report.add("tools_list", False, f"tools/list failed: {e}")
        return
    if not isinstance(tools, list):
        report.add("tools_list", False, "tools/list did not return a list")
        return

    names: list[str] = []
    unnamed = 0
    for tool in tools:
        if isinstance(tool, dict) and isinstance(tool.get("name"), str):
            names.append(tool["name"])
        else:
            unnamed += 1
    report.tools = [n for n in names if not n.startswith("__")]
    report.add("tools_list", unnamed == 0, f"{len(names)} tool(s): {', '.join(names) or 'none'}")

    _check_tool_names(report, names)
    _check_schemas(report, tools)
    _check_declaration(report, manifest, names)
    await _check_health(report, client, names, health_timeout_s)


def _check_tool_names(report: VerifyReport, names: list[str]) -> None:
    bad = [n for n in names if not TOOL_RE.match(n) and n != HEALTH_TOOL]
    reserved = [n for n in names if n.startswith("__") and n != HEALTH_TOOL]
    problems = [*(f"{n}: not a valid tool name" for n in bad), *(f"{n}: reserved" for n in reserved)]
    report.add(
        "tool_names",
        not problems,
        "; ".join(problems) if problems else "all names are valid and unreserved",
    )


def _check_schemas(report: VerifyReport, tools: list[Any]) -> None:
    missing = [
        t.get("name", "?")
        for t in tools
        if isinstance(t, dict) and not isinstance(t.get("inputSchema"), dict)
    ]
    report.add(
        "tool_schemas",
        not missing,
        # Without a schema the model has to guess the arguments, and the gate has
        # nothing to constrain.
        f"no inputSchema: {', '.join(missing)}" if missing else "every tool declares an inputSchema",
        severity="warning",
    )


def _check_declaration(report: VerifyReport, manifest: Manifest, names: list[str]) -> None:
    live = set(names)
    declared = set(manifest.tools)
    absent = sorted(declared - live)
    report.add(
        "manifest_tools",
        not absent,
        # A policy rule naming a tool the module does not have is a rule that
        # silently never applies.
        f"declared but not offered: {', '.join(absent)}" if absent else f"{len(declared)} declared",
    )
    undeclared = sorted(n for n in live - declared if not n.startswith("__"))
    report.add(
        "manifest_complete",
        not undeclared,
        f"offered but not declared: {', '.join(undeclared)}" if undeclared else "manifest lists every tool",
        severity="warning",
    )


async def _check_health(
    report: VerifyReport, client: Any, names: list[str], timeout_s: float
) -> None:
    if HEALTH_TOOL not in names:
        report.add("health_tool", False, f"{HEALTH_TOOL} is required and was not offered")
        return
    report.add("health_tool", True, f"{HEALTH_TOOL} is offered")

    try:
        raw = await client.call_tool(HEALTH_TOOL, {}, timeout_s)
    except TimeoutError:
        report.add("health_call", False, f"no answer within {timeout_s:.0f}s")
        return
    except Exception as e:
        report.add("health_call", False, str(e))
        return
    if not isinstance(raw, dict):
        report.add("health_call", False, "tools/call result is not an object")
        return
    if raw.get("isError"):
        report.add("health_call", False, f"returned an error: {_extract(raw)}")
        return
    report.add("health_call", True, f"answered within {timeout_s:.0f}s")

    payload = _extract(raw)
    if isinstance(payload, dict) and set(payload) == {"result"}:
        payload = payload["result"]  # FastMCP wraps a scalar return
    if not isinstance(payload, dict):
        report.add("health_shape", False, f"expected an object, got {type(payload).__name__}")
        return
    if not isinstance(payload.get("ok"), bool):
        report.add("health_shape", False, "the payload has no boolean 'ok' field")
        return
    report.add("health_shape", True, "{ok: bool, backend, latency_ms, detail}")

    absent = [f for f in HEALTH_FIELDS if f not in payload]
    report.add(
        "health_fields",
        not absent,
        f"not reported: {', '.join(absent)}" if absent else "all documented fields present",
        severity="warning",
    )
    ok = bool(payload.get("ok"))
    report.add(
        "health_ok",
        ok,
        # A module whose backend is down is behaving correctly by saying so, so
        # this is never a contract failure.
        "backend reachable" if ok else f"module reports unhealthy: {payload.get('detail')}",
        severity="warning",
    )


async def _check_shutdown(report: VerifyReport, process: Any) -> None:
    """Closing stdin is how the hub asks a module to stop. A module that ignores
    it gets killed, which is survivable but loses whatever it was doing."""
    if process.returncode is not None:
        report.add("shutdown", False, f"exited early with code {process.returncode}")
        return
    with contextlib.suppress(Exception):
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except Exception:
        report.add("shutdown", False, "still running 5s after stdin closed", severity="warning")
        return
    report.add("shutdown", True, f"exited with code {process.returncode}", severity="warning")


# --------------------------------------------------------------------------
# process plumbing
# --------------------------------------------------------------------------
def _new_client(name: str, process: Any, recorder: _Recorder) -> Any:
    # Imported here so that listing or installing modules does not drag in the
    # hub runtime, and so an import problem in the core surfaces while verifying
    # rather than at `vahub module list`.
    from vahub.core.mcpclient import McpClient

    return McpClient(name, process.stdin, process.stdout, recorder)


def _child_env(manifest: Manifest, environ: Mapping[str, str]) -> dict[str, str]:
    """The same minimal environment the supervisor builds: only the keys the
    manifest declares. Verifying with a fuller environment would pass a module
    that only works because it read something it never declared."""
    env = {
        "PATH": environ.get("PATH", "/usr/bin:/bin"),
        "HOME": environ.get("HOME", "/tmp"),
        "LANG": environ.get("LANG", "C.UTF-8"),
    }
    for key in (*manifest.config.required, *manifest.config.optional):
        if key in environ:
            env[key] = environ[key]
    if manifest.runtime.pythonpath:
        env["PYTHONPATH"] = manifest.runtime.pythonpath
    return env


async def _drain(stream: Any, sink: collections.deque[str]) -> None:
    if stream is None:
        return
    try:
        while True:
            line = await stream.readline()
            if not line:
                return
            sink.append(line.rstrip().decode("utf-8", "replace"))
    except asyncio.CancelledError:
        raise
    except Exception:  # diagnostics must not break the run
        return


async def _kill(process: Any) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except Exception:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()


def _extract(raw: dict[str, Any]) -> Any:
    """Pull a payload out of an MCP tools/call result. The result is untrusted,
    so every branch tolerates the wrong type rather than raising."""
    if raw.get("structuredContent") is not None:
        return raw["structuredContent"]
    parts = []
    content = raw.get("content")
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    text = "\n".join(parts).strip()
    if text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
    return text
