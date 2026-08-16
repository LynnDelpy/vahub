"""`vahub init`: the setup wizard.

It asks the few questions that cannot be guessed (which model provider, which
modules, whether the hub is reachable from the network), then writes two files:
`vahub.yaml`, which is readable and may be committed, and a companion `.env`
holding the credentials, created with mode 600. No secret is ever written into
the YAML and no secret is ever echoed back to the terminal.

Two things are deliberate. The generated policy allows the read-only tools of
the modules that were installed and requires a confirmation for everything that
writes or destroys, because a starter policy that is convenient on day one is
the one nobody revisits. And the wizard ends by running the same checks as
`vahub doctor`, so the first thing a new installation sees is the same report it
will see later.

The credential for the language model is referenced as ${VAHUB_LLM__API_KEY}
rather than the ${VAHUB_LLM_API_KEY} spelling used in the config docstring: the
loader turns every VAHUB_* variable into a config key, and the single underscore
form resolves to a top level `llm_api_key` that strict validation then rejects.
The double underscore form is the documented nesting separator, so it both
interpolates and overrides `llm.api_key` correctly.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
import yaml
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from vahub.cli.doctor import render, run_checks
from vahub.cli.module import installer_for, manifests, outcome, registry_for
from vahub.config.models import Config, ConfigError
from vahub.contracts.manifest import Manifest
from vahub.modules.installer import InstallError
from vahub.modules.registry_client import RegistryError

if TYPE_CHECKING:  # annotation only; a runtime import of main would be a cycle
    from vahub.cli.main import CliState

console = Console()
err = Console(stderr=True)

LLM_KEY_VAR = "VAHUB_LLM__API_KEY"
SECRET_LOOKING = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
# Values that survive both `set -a; . file` and a systemd EnvironmentFile= unquoted.
PLAIN_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./~-]*$")
ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


@dataclass(frozen=True)
class ProviderChoice:
    key: str
    label: str
    provider: str
    base_url: str
    model: str
    needs_key: bool
    note: str


PROVIDERS: tuple[ProviderChoice, ...] = (
    ProviderChoice(
        "anthropic", "Anthropic", "anthropic", "https://api.anthropic.com/v1",
        "claude-opus-5", True, "an API key from the Anthropic console",
    ),
    ProviderChoice(
        "openrouter", "OpenRouter", "openai_compat", "https://openrouter.ai/api/v1",
        "anthropic/claude-opus-5", True, "one key, many models",
    ),
    ProviderChoice(
        "openai", "OpenAI", "openai_compat", "https://api.openai.com/v1",
        "", True, "you will be asked for the model name",
    ),
    ProviderChoice(
        "ollama", "Ollama on this machine", "openai_compat", "http://localhost:11434/v1",
        "", False, "no key, nothing leaves the machine",
    ),
    ProviderChoice(
        "custom", "Another OpenAI compatible endpoint", "openai_compat", "",
        "", True, "llama.cpp, vLLM, LM Studio, a company gateway",
    ),
    ProviderChoice(
        "mock", "None for now (mock)", "mock", "https://openrouter.ai/api/v1",
        "mock", False, "canned replies, for trying the hub out",
    ),
)


@dataclass
class Layout:
    config_path: Path
    secrets_path: Path
    state_dir: Path
    modules_dir: Path


def default_layout() -> Layout:
    """Root installs into /etc and /var; anyone else works in the current directory.

    The loader searches ./vahub.yaml before /etc/vahub/vahub.yaml, so the
    unprivileged layout is found without an extra --config on every command.
    """
    if os.geteuid() == 0:
        etc = Path("/etc/vahub")
        return Layout(etc / "vahub.yaml", etc / ".env", Path("/var/lib/vahub"), etc / "modules.d")
    here = Path.cwd()
    return Layout(here / "vahub.yaml", here / ".env", here / "state", here / "state" / "modules.d")


def local_timezone() -> str:
    for candidate in (os.environ.get("TZ"), _read_etc_timezone()):
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return candidate
    return "UTC"


def _read_etc_timezone() -> str | None:
    path = Path("/etc/timezone")
    if path.is_file():
        return path.read_text().strip() or None
    link = Path("/etc/localtime")
    if link.is_symlink():
        target = str(link.readlink())
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    return None


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------
def init(
    ctx: typer.Context,
    non_interactive: bool = typer.Option(
        False, "--non-interactive", "-n", help="Ask nothing; use the flags and the defaults."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing configuration."),
    llm_provider: str = typer.Option(
        None, "--llm-provider", help="anthropic, openrouter, openai, ollama, custom or mock."
    ),
    llm_base_url: str = typer.Option(None, "--llm-base-url", help="Override the provider's base URL."),
    llm_model: str = typer.Option(None, "--llm-model", help="Model name to request."),
    api_key_stdin: bool = typer.Option(
        False, "--api-key-stdin", help="Read the model API key from standard input."
    ),
    modules: list[str] = typer.Option(
        None, "--module", "-m", help="Install this module (repeatable)."
    ),
    expose: str = typer.Option(None, "--expose", help="loopback (default) or lan."),
    host: str = typer.Option(None, "--host", help="Bind address, implies --expose lan if not loopback."),
    port: int = typer.Option(None, "--port", help="Port to listen on."),
    origin: str = typer.Option(None, "--origin", help="Browser origin allowed to call the API."),
    timezone: str = typer.Option(None, "--timezone", help="IANA timezone for schedules."),
    state_dir: Path = typer.Option(None, "--state-dir", help="Where runtime state lives."),
    modules_dir: Path = typer.Option(None, "--modules-dir", help="Where module manifests live."),
    registry_url: str = typer.Option(None, "--registry", help="Registry index URL or file."),
    allow_root: bool = typer.Option(False, "--allow-root", help="Permit installing modules as root."),
    no_network: bool = typer.Option(
        False, "--no-network", help="Do not contact the registry or the model endpoint."
    ),
) -> None:
    """Create a configuration, install modules and check the result."""
    cli: CliState = ctx.obj
    interactive = not non_interactive

    layout = default_layout()
    if cli.config_path is not None:
        layout.config_path = cli.config_path
        layout.secrets_path = cli.config_path.parent / ".env"
    if state_dir is not None:
        layout.state_dir = state_dir
    if modules_dir is not None:
        layout.modules_dir = modules_dir

    console.print("[bold]vahub setup[/bold]")
    console.print(f"Configuration will be written to {layout.config_path}.\n")

    if layout.config_path.exists() and not force:
        if not interactive:
            err.print(
                f"[red]error:[/red] {layout.config_path} already exists. "
                "Pass --force to replace it (a timestamped copy is kept)."
            )
            raise typer.Exit(1)
        console.print(f"[yellow]{layout.config_path} already exists.[/yellow]")
        if not Confirm.ask("Replace it? The current file is copied aside first", default=False):
            console.print("Keeping the existing configuration. Nothing was changed.")
            console.print("Check it with [bold]vahub doctor[/bold].")
            return

    # 1. language model -----------------------------------------------------
    choice = _pick_provider(llm_provider, interactive)
    base_url = llm_base_url or choice.base_url
    if not base_url:
        base_url = _ask("Base URL of the endpoint", default="http://localhost:8080/v1",
                        interactive=interactive)
    model = llm_model or choice.model
    if not model:
        model = _ask("Model to request", default="", interactive=interactive)
        if not model:
            raise ConfigError("no model was given; pass --llm-model")

    secrets: dict[str, str] = {}
    api_key = _read_api_key(choice, interactive=interactive, from_stdin=api_key_stdin)
    if api_key:
        secrets[LLM_KEY_VAR] = api_key

    # 2. modules ------------------------------------------------------------
    # The installer needs somewhere to work before there is a file to read, so
    # the answers so far are validated into a config and used directly.
    config = _draft_config(
        provider=choice.provider, base_url=base_url, model=model, has_key=bool(choice.needs_key),
        timezone=timezone or _ask_timezone(interactive), layout=layout,
    )
    _prepare_directories(layout)
    chosen = _pick_modules(config, list(modules or []), interactive, registry_url, offline=no_network)
    installed = _install_modules(
        config, chosen, secrets, interactive, offline=no_network, allow_root=allow_root
    )

    # 3. exposure -----------------------------------------------------------
    web = _pick_exposure(expose, host, port, origin, interactive)

    # 4. write --------------------------------------------------------------
    text = _render_config(
        timezone=config.hub.timezone, layout=layout, web=web,
        provider=choice.provider, base_url=base_url, model=model, has_key=bool(choice.needs_key),
        policy=_starter_policy(installed),
    )
    _backup(layout.config_path)
    _write_file(layout.config_path, text, 0o644)
    console.print(f"\n[green]wrote[/green] {layout.config_path}")

    if secrets:
        _write_env_file(layout.secrets_path, secrets)
        console.print(f"[green]wrote[/green] {layout.secrets_path} (mode 600, {len(secrets)} entries)")
        # The closing checks are worth more if they run against the real
        # credentials, and this process is about to exit anyway.
        os.environ.update(secrets)

    # 5. check and hand over ------------------------------------------------
    console.print("\n[bold]Checking the result[/bold]")
    try:
        checked = _reload(layout.config_path)
    except ConfigError as e:
        err.print(f"[red]the generated configuration does not load:[/red]\n{e}")
        raise typer.Exit(1) from e

    failures = render(run_checks(checked, config_path=layout.config_path, offline=no_network), console)
    _next_steps(layout, secrets, installed)
    if failures:
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# questions
# --------------------------------------------------------------------------
def _ask(question: str, *, default: str, interactive: bool) -> str:
    if not interactive:
        return default
    return Prompt.ask(question, default=default) if default else Prompt.ask(question)


def _pick_provider(flag: str | None, interactive: bool) -> ProviderChoice:
    by_key = {p.key: p for p in PROVIDERS}
    if flag:
        if flag not in by_key:
            raise ConfigError(f"unknown --llm-provider {flag!r}; one of: {', '.join(by_key)}")
        return by_key[flag]
    if not interactive:
        return by_key["mock"]

    console.print("[bold]Which language model should the agent use?[/bold]")
    table = Table(box=None, show_header=False, pad_edge=False)
    for index, provider in enumerate(PROVIDERS, start=1):
        table.add_row(f"  {index}.", provider.label, Text(provider.note, style="dim"))
    console.print(table)
    answer = Prompt.ask(
        "Choice", choices=[str(i) for i in range(1, len(PROVIDERS) + 1)], default="1", show_choices=False
    )
    return PROVIDERS[int(answer) - 1]


def _read_api_key(choice: ProviderChoice, *, interactive: bool, from_stdin: bool) -> str | None:
    """Collect the key without echoing it and without accepting it on argv.

    A key passed as a command line flag ends up in the shell history and in the
    process list of every other user on the machine, so there is no such flag:
    interactively it is typed blind, and for scripts it comes from stdin or from
    the environment.
    """
    if not choice.needs_key:
        return None
    if from_stdin:
        import sys

        key = sys.stdin.readline().strip()
        if not key:
            raise ConfigError("--api-key-stdin was given but nothing was read from standard input")
        return key
    if os.environ.get(LLM_KEY_VAR):
        console.print(f"Using the API key already set in {LLM_KEY_VAR}.")
        return os.environ[LLM_KEY_VAR]
    if not interactive:
        console.print(
            f"[yellow]No API key given.[/yellow] The config will reference ${{{LLM_KEY_VAR}}}; "
            "set it before starting the hub."
        )
        return None

    console.print(f"\n{choice.label} needs {choice.note}. It is stored in the .env file, not in vahub.yaml.")
    key = Prompt.ask("API key (not shown as you type)", password=True, default="", show_default=False)
    key = key.strip()
    if not key:
        console.print(f"[yellow]Left empty.[/yellow] Put the key in {LLM_KEY_VAR} before starting.")
        return None
    return key


def _ask_timezone(interactive: bool) -> str:
    default = local_timezone()
    while True:
        answer = _ask("Timezone for schedules", default=default, interactive=interactive)
        try:
            ZoneInfo(answer)
            return answer
        except (ZoneInfoNotFoundError, ValueError):
            if not interactive:
                raise ConfigError(f"{answer!r} is not a known IANA timezone") from None
            console.print(f"[yellow]{answer} is not a known IANA timezone.[/yellow] Try Europe/Berlin.")


def _pick_modules(
    config: Config, flags: list[str], interactive: bool, registry_url: str | None, *, offline: bool
) -> list[str]:
    if flags or offline or not interactive:
        return flags

    console.print("\n[bold]Which modules should the hub be able to use?[/bold]")
    try:
        entries = registry_for(config, registry_url).search("")
    except RegistryError as e:
        console.print(f"[yellow]The module catalog is unavailable:[/yellow] {e}")
        console.print("Continuing without modules. Add them later with `vahub module add`.")
        return []

    if not entries:
        console.print("The registry lists no modules.")
        return []

    table = Table(box=None, show_header=False, pad_edge=False)
    for index, (name, entry) in enumerate(entries, start=1):
        table.add_row(f"  {index}.", Text(name), Text(entry.description, style="dim"))
    console.print(table)
    answer = Prompt.ask("Numbers, comma separated (empty for none)", default="", show_default=False)

    picked: list[str] = []
    for part in answer.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= len(entries):
            console.print(f"[yellow]ignoring {part!r}[/yellow]")
            continue
        picked.append(entries[int(part) - 1][0])
    return picked


def _pick_exposure(
    expose: str | None, host: str | None, port: int | None, origin: str | None, interactive: bool
) -> dict[str, Any]:
    chosen_port = port or 8080
    if host:
        chosen_host = host
    elif expose in ("lan", "network"):
        chosen_host = "0.0.0.0"
    elif expose in ("loopback", "local", None) and not interactive:
        chosen_host = "127.0.0.1"
    else:
        console.print("\n[bold]Who should be able to reach the web interface?[/bold]")
        console.print(
            "  1. This machine only (127.0.0.1)\n"
            "  2. The local network (0.0.0.0)",
        )
        console.print(
            Text(
                "vahub has no login of its own: on the network, anyone who can reach the port can "
                "control whatever the policy allows.",
                style="dim",
            )
        )
        answer = Prompt.ask("Choice", choices=["1", "2"], default="1", show_choices=False)
        chosen_host = "127.0.0.1" if answer == "1" else "0.0.0.0"

    if origin:
        origins = [origin]
    elif chosen_host in ("127.0.0.1", "::1", "localhost"):
        origins = [f"http://localhost:{chosen_port}"]
    else:
        suggestion = f"http://{_hostname()}:{chosen_port}"
        origins = [_ask("Browser origin you will open the UI from", default=suggestion,
                        interactive=interactive)]
        console.print(
            "[yellow]Reminder:[/yellow] put an authenticating reverse proxy in front of this port."
        )
    return {"host": chosen_host, "port": chosen_port, "origin_allowlist": origins}


def _hostname() -> str:
    import socket

    return socket.gethostname() or "localhost"


# --------------------------------------------------------------------------
# installing
# --------------------------------------------------------------------------
def _draft_config(
    *, provider: str, base_url: str, model: str, has_key: bool, timezone: str, layout: Layout
) -> Config:
    """The in memory config the installer needs before the file exists."""
    return Config.model_validate(
        {
            "hub": {
                "timezone": timezone,
                "state_dir": str(layout.state_dir),
                "modules_dir": str(layout.modules_dir),
            },
            "llm": {
                "provider": provider,
                "base_url": base_url,
                "model": model,
                "api_key": f"${{{LLM_KEY_VAR}}}" if has_key else None,
            },
        }
    )


def _prepare_directories(layout: Layout) -> None:
    for directory in (layout.config_path.parent, layout.state_dir, layout.modules_dir):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            raise ConfigError(
                f"cannot create {directory}: {e}. Run as root for a system install, or pass "
                "--state-dir and --modules-dir pointing somewhere writable."
            ) from e


def _install_modules(
    config: Config, names: list[str], secrets: dict[str, str], interactive: bool,
    *, offline: bool, allow_root: bool,
) -> dict[str, Manifest]:
    """Install the chosen modules and collect what each of them needs.

    One module that fails to install does not end the setup: the rest are
    installed, the failure is reported, and the generated policy simply has no
    rules for the module that is not there.
    """
    if not names:
        return manifests(config)

    inst = installer_for(config, offline=offline, allow_root=allow_root)
    for name in names:
        console.print(f"\nInstalling {name} ...")
        try:
            result = inst.install(name)
        except (InstallError, RegistryError) as e:
            err.print(f"[red]{name} was not installed:[/red] ", Text(str(e)), sep="")
            continue
        console.print(f"[green]{outcome(result)}[/green]")
        for warning in result.warnings:
            err.print("[yellow]note:[/yellow] ", Text(warning), sep="")
        keys = sorted({*result.required_config, *result.manifest.config.required})
        _ask_module_config(result.name, keys, secrets, interactive)
    return manifests(config)


def _ask_module_config(
    module: str, keys: list[str], secrets: dict[str, str], interactive: bool
) -> None:
    for key in keys:
        if key.startswith("VAHUB_"):
            # Every VAHUB_* variable is also a config override, so a module key
            # in that namespace would collide with the hub's own settings.
            console.print(f"[yellow]{module} asks for {key}, which is reserved by the hub.[/yellow]")
            continue
        if os.environ.get(key):
            # Recorded rather than relied on: the value is in this shell, but the
            # hub is started from a service manager that never saw it.
            secrets[key] = os.environ[key]
            console.print(f"  {key} is set in this environment; copying it into the credentials file.")
            continue
        if not interactive:
            console.print(f"  [yellow]{key} is not set[/yellow]; the module cannot start without it.")
            continue
        secret = bool(SECRET_LOOKING.search(key))
        prompt = f"  {module}: {key}" + (" (not shown as you type)" if secret else "")
        value = Prompt.ask(prompt, password=secret, default="", show_default=False).strip()
        if value:
            secrets[key] = value
        else:
            console.print(f"  [yellow]left empty[/yellow]; put {key} in the .env file before starting.")


# --------------------------------------------------------------------------
# generating vahub.yaml
# --------------------------------------------------------------------------
def _starter_policy(modules: dict[str, Manifest]) -> dict[str, Any]:
    """Read tools allowed, everything that changes something confirmed.

    A rule with no `constraints` permits the tool only when it is called with no
    arguments, because the gate rejects any argument it was not told about. The
    generated file says so; there is no way to derive argument constraints from
    a manifest, which only declares names and classes.
    """
    rules: dict[str, Any] = {}
    for name, manifest in sorted(modules.items()):
        for tool, spec in sorted(manifest.tools.items()):
            rules[f"{name}.{tool}"] = {"class": spec.cls}
    return {
        "default": "deny",
        "confirm_ttl_s": 60.0,
        # The three principals that actually act: the agent, the scheduler and
        # the development endpoint. A principal can only subtract or escalate,
        # so listing one that never appears would be decoration.
        "principals": {
            "agent": {"confirm": ["write", "destructive"], "deny": []},
            "scheduler": {"confirm": ["destructive"], "deny": ["*.lock_*", "*.unlock_*", "*.delete_*"]},
            "dev": {"confirm": ["destructive"], "deny": []},
        },
        "rules": rules,
    }


class _Dumper(yaml.SafeDumper):
    """Indents list items under their key. The generated file is meant to be
    read and edited by hand, and the default flush-left dashes read badly."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow=flow, indentless=False)


def _block(name: str, data: Any) -> str:
    return yaml.dump(
        {name: data}, Dumper=_Dumper, sort_keys=False, width=100, default_flow_style=False
    )


def _render_config(
    *, timezone: str, layout: Layout, web: dict[str, Any], provider: str, base_url: str, model: str,
    has_key: bool, policy: dict[str, Any],
) -> str:
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    credentials = (
        "#\n"
        "# No credential belongs in this file. Values written as ${NAME} are read\n"
        f"# from the environment; `vahub init` put them in {layout.secrets_path}\n"
        "# with mode 600. Load that file before starting the hub (systemd:\n"
        "# EnvironmentFile=).\n"
        if has_key
        else ""
    )
    parts = [
        f"# vahub configuration, generated by `vahub init` on {generated}.\n"
        + credentials
        + "#\n"
        "# Every setting can also be overridden with a VAHUB_ variable, using __\n"
        "# between levels: VAHUB_WEB__PORT=9000 sets web.port.\n",
        _block(
            "hub",
            {
                "log_level": "INFO",
                "log_format": "json",
                "timezone": timezone,
                "state_dir": str(layout.state_dir),
                "modules_dir": str(layout.modules_dir),
            },
        ),
        "# The hub has no authentication of its own. On 127.0.0.1 only this machine\n"
        "# can reach it. On any other address, put an authenticating reverse proxy in\n"
        "# front of it. origin_allowlist is checked on API calls and on WebSocket\n"
        "# handshakes; \"*\" turns that check off.\n"
        + _block(
            "web",
            {
                "host": web["host"],
                "port": web["port"],
                "origin_allowlist": web["origin_allowlist"],
            },
        ),
        (f"# api_key is a reference, not the key. The value lives in {layout.secrets_path}.\n"
         if has_key else "")
        + _block(
            "llm",
            {
                "provider": provider,
                "base_url": base_url,
                "model": model,
                **({"api_key": "${" + LLM_KEY_VAR + "}"} if has_key else {}),
                "temperature": 0.2,
                "max_tokens": 1024,
            },
        ),
        "# One turn cannot cost more than this. tool_result_bytes matters most: a\n"
        "# single unfiltered tool result can otherwise dwarf a whole conversation.\n"
        + _block(
            "budgets",
            {"iterations_per_turn": 8, "tool_result_bytes": 16384, "tokens_per_turn": 20000},
        ),
        '# "browser" means speech happens in the browser: no audio leaves the machine\n'
        "# and no credentials are needed.\n"
        + _block("speech", {"stt": {"provider": "browser"}, "tts": {"provider": "browser"}}),
        "# The gate. Checked in code on every tool call, including the scheduler's.\n"
        "#\n"
        "# Read tools are allowed. Everything that writes or destroys needs a\n"
        "# confirmation from a human, which the scheduler cannot give, so unattended\n"
        "# runs cannot perform a destructive action.\n"
        "#\n"
        "# A rule without `constraints` permits the tool only when it is called with\n"
        "# no arguments: an argument the rule does not describe is rejected. Describe\n"
        "# the arguments you want to allow, for example\n"
        "#\n"
        "#   homeassistant.light_turn_on:\n"
        "#     class: write\n"
        "#     constraints:\n"
        '#       entity_id: { matches: "^light\\\\.", max_len: 64 }\n'
        "#       brightness_pct: { range: [1, 100] }\n"
        + _block("policy", policy),
        "# Deterministic routines. They run through the gate as principal=scheduler,\n"
        "# without the agent and without the model.\n"
        "#\n"
        "# schedules:\n"
        "#   - id: morning\n"
        '#     cron: "30 6 * * 1-5"\n'
        "#     steps:\n"
        "#       - module: homeassistant\n"
        "#         tool: light_turn_on\n"
        "#         args: { entity_id: light.bedroom }\n"
        + _block("schedules", []),
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# writing files
# --------------------------------------------------------------------------
def _backup(path: Path) -> None:
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    copy = path.with_name(f"{path.name}.{stamp}.bak")
    copy.write_bytes(path.read_bytes())
    copy.chmod(path.stat().st_mode & 0o777)
    console.print(f"kept the previous file as {copy}")


def _write_file(path: Path, text: str, mode: int) -> None:
    """Write through a temporary file in the same directory, then rename.

    mkstemp creates the file readable only by its owner, so a secrets file never
    exists world readable even for the moment before the chmod, and an
    interrupted run leaves the previous file intact rather than a truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _env_line(key: str, value: str) -> str:
    if PLAIN_VALUE.match(value):
        return f"{key}={value}"
    quoted = value.replace("'", "'\\''")
    return f"{key}='{quoted}'"


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    """Update our own keys and leave every other line of the file alone.

    Existing values are never parsed, only their key is read, so a credential
    this run did not set is carried over verbatim and is never held in memory.
    """
    header = [
        "# Credentials for vahub. Referenced from vahub.yaml as ${NAME}.",
        "# Load before starting the hub:",
        "#   systemd:  EnvironmentFile=" + str(path),
        "#   a shell:  set -a; . " + str(path) + "; set +a",
        "# Keep this file at mode 600.",
    ]

    existing: list[str] = []
    if path.exists():
        existing = path.read_text().splitlines()
        _backup(path)

    remaining = dict(values)
    body: list[str] = []
    for line in existing:
        match = ENV_LINE.match(line)
        if match is None:
            continue  # comments and blanks are regenerated from the header
        key = match.group(1)
        body.append(_env_line(key, remaining.pop(key)) if key in remaining else line)
    body += [_env_line(key, value) for key, value in remaining.items()]

    _write_file(path, "\n".join([*header, "", *body]) + "\n", stat.S_IRUSR | stat.S_IWUSR)


def _reload(path: Path) -> Config:
    from vahub.config.loader import load_config

    return load_config(path)


# --------------------------------------------------------------------------
# closing
# --------------------------------------------------------------------------
def _next_steps(layout: Layout, secrets: dict[str, str], modules: dict[str, Manifest]) -> None:
    console.print("\n[bold]Next[/bold]")
    load = ""
    if secrets:
        load = f"set -a; . {layout.secrets_path}; set +a; "
    console.print(f"  Start it:      {load}vahub --config {layout.config_path} run")
    console.print(f"  Check it:      vahub --config {layout.config_path} doctor")
    console.print("  Add a module:  vahub module search")
    if modules:
        tools = sum(len(m.tools) for m in modules.values())
        console.print(
            f"\n  {len(modules)} module(s), {tools} tools. Read tools are allowed; writing and "
            "destructive tools ask for a confirmation."
        )
        console.print(
            f"  Edit the rules in {layout.config_path} to describe the arguments each tool may take."
        )
    if secrets:
        console.print(
            f"\n  Under systemd, point the unit at the credentials with "
            f"EnvironmentFile={layout.secrets_path}."
        )
