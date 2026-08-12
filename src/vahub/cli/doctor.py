"""`vahub doctor`: everything that can be checked without starting the hub.

The output is a checklist, not a log: every line names one thing, says whether
it is fine, and (when it is not) says what to do about it. The checks run in one
pass and return data, so `vahub init` can end with the same report instead of a
second, differently worded opinion.

A failure means the hub will not work correctly and the exit code is non-zero. A
warning means it will run but something is worth knowing, most importantly the
combinations that expose an unauthenticated hub to a network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import typer
from rich.console import Console
from rich.text import Text

from vahub.cli.module import installed_modules, manifests, secrets_hint, static_findings
from vahub.config.models import Config, ConfigError
from vahub.contracts.manifest import HEALTH_TOOL

if TYPE_CHECKING:  # annotation only; a runtime import of main would be a cycle
    from vahub.cli.main import CliState

console = Console()
err = Console(stderr=True)

Status = Literal["ok", "warn", "fail"]

SECTIONS = ("Configuration", "State", "Modules", "Policy", "Language model", "Exposure", "Schedules")


@dataclass
class Check:
    section: str
    name: str
    status: Status
    detail: str = ""
    hint: str = ""


def doctor(
    ctx: typer.Context,
    offline: bool = typer.Option(False, "--offline", help="Skip the checks that need the network."),
) -> None:
    """Validate the installation and report what needs fixing."""
    cli: CliState = ctx.obj
    try:
        config = cli.load()
    except ConfigError as e:
        # Reporting the problem is doctor's job, so it does not re-raise into
        # the top-level handler that other commands rely on.
        render([Check("Configuration", str(cli.path), "fail", str(e))], console)
        raise typer.Exit(1) from e

    checks = run_checks(config, config_path=cli.path, offline=offline)
    if render(checks, console) > 0:
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------
def run_checks(config: Config, *, config_path: Path, offline: bool = False) -> list[Check]:
    checks: list[Check] = []
    checks += _config_checks(config, config_path)
    checks += _state_checks(config)
    checks += _module_checks(config)
    checks += _policy_checks(config)
    checks += _llm_checks(config, offline=offline)
    checks += exposure_checks(config)
    checks += _schedule_checks(config)
    return checks


def _config_checks(config: Config, config_path: Path) -> list[Check]:
    checks = [
        Check(
            "Configuration", "config file",
            "ok" if config_path.is_file() else "warn",
            str(config_path) if config_path.is_file() else f"{config_path} does not exist",
            "" if config_path.is_file() else "run `vahub init` to create one",
        )
    ]
    try:
        ZoneInfo(config.hub.timezone)
        checks.append(Check("Configuration", "timezone", "ok", config.hub.timezone))
    except (ZoneInfoNotFoundError, ValueError):
        checks.append(
            Check(
                "Configuration", "timezone", "fail", f"{config.hub.timezone} is not a known zone",
                "use an IANA name such as Europe/Berlin; install tzdata if the database is missing",
            )
        )

    secrets = secrets_hint(config_path)
    if secrets.is_file():
        mode = secrets.stat().st_mode & 0o777
        checks.append(
            Check(
                "Configuration", "secrets file",
                "ok" if mode <= 0o600 else "warn",
                f"{secrets} (mode {mode:o})",
                "" if mode <= 0o600 else f"chmod 600 {secrets}: it holds credentials",
            )
        )
    return checks


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


def _state_checks(config: Config) -> list[Check]:
    checks: list[Check] = []
    for label, directory in (("state dir", config.hub.state_dir), ("modules dir", config.hub.modules_dir)):
        if directory.is_dir():
            checks.append(
                Check(
                    "State", label,
                    "ok" if _writable(directory) else "fail",
                    str(directory),
                    "" if _writable(directory) else f"the hub user cannot write to {directory}",
                )
            )
            continue
        parent = directory.parent
        creatable = parent.is_dir() and _writable(parent)
        checks.append(
            Check(
                "State", label,
                "warn" if creatable else "fail",
                f"{directory} does not exist yet",
                "" if creatable else f"create it, or point the config at a writable path ({parent} is not)",
            )
        )

    db = config.hub.db_path
    if db.exists():
        checks.append(
            Check(
                "State", "database",
                "ok" if _writable(db) else "fail",
                str(db),
                "" if _writable(db) else f"the hub user cannot write to {db}",
            )
        )
    return checks


def _module_checks(config: Config) -> list[Check]:
    modules = installed_modules(config)
    if not modules:
        return [
            Check(
                "Modules", "installed", "warn", "none",
                "the agent has nothing to call; try `vahub module search`",
            )
        ]

    checks: list[Check] = []
    for module in modules:
        findings = static_findings(config, module)
        if not findings:
            tools = len(module.manifest.tools) if module.manifest else 0
            checks.append(Check("Modules", module.name, "ok", f"{module.version}, {tools} tools"))
            continue
        for level, message in findings:
            checks.append(Check("Modules", module.name, "fail" if level == "fail" else "warn", message))
    return checks


def _policy_checks(config: Config) -> list[Check]:
    policy = config.policy
    declared = manifests(config)
    checks: list[Check] = []

    if policy.default == "allow":
        checks.append(
            Check(
                "Policy", "default", "warn", "allow",
                "every tool not otherwise denied is callable; default: deny is the safer shape",
            )
        )
    else:
        checks.append(Check("Policy", "default", "ok", "deny"))

    if not policy.rules:
        checks.append(
            Check(
                "Policy", "rules", "warn", "none",
                "with default deny the agent cannot call anything; add rules for the tools you want",
            )
        )
    else:
        checks.append(Check("Policy", "rules", "ok", _plural(len(policy.rules), "rule")))

    known = {f"{name}.{tool}" for name, m in declared.items() for tool in m.tools}
    unknown = sorted(key for key in policy.rules if key not in known and "." in key)
    if unknown:
        checks.append(
            Check(
                "Policy", "unknown tools", "warn", ", ".join(unknown),
                "no installed module declares these; a rule that matches nothing is usually a typo",
            )
        )

    destructive = sorted(
        key for key, rule in policy.rules.items() if rule.cls == "destructive"
    )
    if destructive:
        unconfirmed = sorted(
            name for name, principal in policy.principals.items()
            if "destructive" not in principal.confirm
        )
        checks.append(
            Check(
                "Policy", "destructive tools",
                "ok" if not unconfirmed else "warn",
                ", ".join(destructive),
                "" if not unconfirmed
                else f"principals {', '.join(unconfirmed)} may call them without confirmation",
            )
        )

    reserved = [key for key in policy.rules if key.endswith(f".{HEALTH_TOOL}")]
    if reserved:
        checks.append(
            Check(
                "Policy", "reserved tool", "warn", ", ".join(reserved),
                f"{HEALTH_TOOL} is called by the hub itself; a rule for it has no effect",
            )
        )
    return checks


def _llm_checks(config: Config, *, offline: bool) -> list[Check]:
    llm = config.llm
    if llm.provider == "mock":
        return [
            Check(
                "Language model", "provider", "warn", "mock",
                "the mock provider replies from a keyword table; set a real provider before relying on it",
            )
        ]

    checks = [Check("Language model", "provider", "ok", f"{llm.provider}, model {llm.model}")]
    if not llm.api_key:
        checks.append(
            Check(
                "Language model", "api key", "fail", "not set",
                "set llm.api_key to ${VAHUB_LLM_API_KEY} and put the key in the secrets file",
            )
        )
        return checks
    checks.append(Check("Language model", "api key", "ok", "set (value not shown)"))

    if offline:
        checks.append(Check("Language model", "endpoint", "warn", "not checked (--offline)"))
        return checks

    status, detail, hint = _probe_llm(llm.provider, llm.base_url, llm.api_key, llm.request_timeout_s)
    checks.append(Check("Language model", "endpoint", status, detail, hint))
    return checks


def _probe_llm(provider: str, base_url: str, api_key: str, timeout_s: float) -> tuple[Status, str, str]:
    """One cheap authenticated GET, to tell "unreachable" from "key rejected"."""
    url = base_url.rstrip("/") + "/models"
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = httpx.get(url, headers=headers, timeout=min(timeout_s, 15.0))
    except httpx.HTTPError as e:
        return "fail", f"{base_url} is not reachable ({e})", "check the URL, DNS and any proxy"
    if response.status_code in (401, 403):
        return (
            "fail",
            f"{base_url} rejected the key (HTTP {response.status_code})",
            "the key is wrong, expired, or lacks access to this model",
        )
    if response.status_code == 404:
        return (
            "warn",
            f"{base_url} has no /models endpoint (HTTP 404)",
            "some compatible servers do not implement it; this does not prove the key is bad",
        )
    if response.status_code >= 400:
        return "warn", f"{base_url} answered HTTP {response.status_code}", "the endpoint is up but unhappy"
    return "ok", f"{base_url} answered and accepted the key", ""


def exposure_checks(config: Config) -> list[Check]:
    web = config.web
    loopback = web.host in ("127.0.0.1", "::1", "localhost")
    checks = [
        Check(
            "Exposure", "bind address",
            "ok" if loopback else "warn",
            f"{web.host}:{web.port}",
            "" if loopback
            else "the hub has no authentication of its own; anyone who can reach this port controls it",
        )
    ]

    wildcard = "*" in web.origin_allowlist
    if wildcard:
        checks.append(
            Check(
                "Exposure", "origin allowlist",
                "warn" if loopback else "fail",
                "*",
                "any web page you visit can drive the hub; list the origins you actually use",
            )
        )
    elif not web.origin_allowlist:
        checks.append(
            Check(
                "Exposure", "origin allowlist", "warn", "empty",
                "no browser origin is allowed; the web UI will be refused",
            )
        )
    else:
        checks.append(Check("Exposure", "origin allowlist", "ok", ", ".join(web.origin_allowlist)))

    if web.dev_tools_endpoint:
        checks.append(
            Check(
                "Exposure", "dev tools endpoint",
                "warn" if loopback else "fail",
                "enabled",
                "it calls tools without the agent and without authentication; "
                "turn it off outside development",
            )
        )
    else:
        checks.append(Check("Exposure", "dev tools endpoint", "ok", "disabled"))

    if not loopback and (wildcard or web.dev_tools_endpoint):
        opened = [
            text for text, on in
            (("origin '*'", wildcard), ("the dev endpoint", web.dev_tools_endpoint)) if on
        ]
        checks.append(
            Check(
                "Exposure", "combination", "fail",
                f"bound to {web.host} with {' and '.join(opened)}",
                "put an authenticating reverse proxy in front, or bind to 127.0.0.1",
            )
        )
    return checks


def _schedule_checks(config: Config) -> list[Check]:
    if not config.schedules:
        return []
    declared = manifests(config)
    checks: list[Check] = []

    for schedule in config.schedules:
        problems: list[str] = []
        try:
            from apscheduler.triggers.cron import CronTrigger

            CronTrigger.from_crontab(schedule.cron, timezone=config.hub.timezone)
        except ImportError:
            pass  # apscheduler is a runtime dependency; not having it here is not the user's problem
        except ValueError as e:
            problems.append(f"cron {schedule.cron!r} is not valid: {e}")

        for step in schedule.steps:
            key = f"{step.module}.{step.tool}"
            if step.module not in declared:
                problems.append(f"step calls {key} but {step.module} is not installed")
            elif step.tool not in declared[step.module].tools:
                problems.append(f"step calls {key} but the module does not declare that tool")
            if config.policy.default != "allow" and key not in config.policy.rules:
                problems.append(f"no policy rule for {key}; the scheduler will be denied")

        if not schedule.steps:
            problems.append("has no steps")

        if problems:
            for problem in problems:
                checks.append(Check("Schedules", schedule.id, "fail", problem))
        else:
            enabled = "enabled" if schedule.enabled else "disabled"
            checks.append(Check("Schedules", schedule.id, "ok", f"{schedule.cron}, {enabled}"))
    return checks


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
_MARK = {"ok": "[green]ok  [/green]", "warn": "[yellow]warn[/yellow]", "fail": "[red]fail[/red]"}
MAX_DETAIL = 220


def _one_line(text: str) -> str:
    """A checklist is one line per item. Some details quote a parser error that
    spans five lines and a caret; those are collapsed rather than allowed to
    break the alignment of everything after them."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= MAX_DETAIL else collapsed[: MAX_DETAIL - 3] + "..."


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def render(checks: list[Check], out: Console) -> int:
    """Print the checklist. Returns the number of failures."""
    order = {section: index for index, section in enumerate(SECTIONS)}
    width = max((len(c.name) for c in checks), default=0)

    for section in sorted({c.section for c in checks}, key=lambda s: order.get(s, len(SECTIONS))):
        out.print(f"\n[bold]{section}[/bold]")
        for check in [c for c in checks if c.section == section]:
            # Detail can carry a module's own text, so it is never parsed as markup.
            out.print(
                f"  {_MARK[check.status]}  {check.name.ljust(width)}  ",
                Text(_one_line(check.detail)),
                sep="",
            )
            if check.hint:
                out.print(" " * (width + 10), Text(_one_line(check.hint), style="dim"), sep="")

    failures = sum(1 for c in checks if c.status == "fail")
    warnings = sum(1 for c in checks if c.status == "warn")
    out.print()
    if failures:
        out.print(
            f"[red]{_plural(failures, 'problem')}[/red] and {_plural(warnings, 'warning')}. "
            "The hub will not work as configured."
        )
    elif warnings:
        out.print(f"[green]No problems[/green], {_plural(warnings, 'warning')} worth reading.")
    else:
        out.print("[green]Everything checks out.[/green]")
    return failures
