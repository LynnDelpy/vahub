"""`vahub run` and `vahub serve`: start the hub in the foreground.

`run` is the form a service manager wants: it logs as configured, it does not
decorate its output, and it exits 0 on SIGTERM. `serve` is the same runtime with
the settings a person at a terminal wants (console logging, the URL printed
once), because the alternative is everyone rediscovering the same three flags.

Signal handlers are installed before the runtime starts. If the runtime installs
its own, they replace these, which is fine: what matters is that a SIGTERM
arriving in the first milliseconds is not lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from pydantic import ValidationError
from rich.console import Console

from vahub.cli.doctor import exposure_checks
from vahub.config.loader import load_config
from vahub.config.models import Config, ConfigError

if TYPE_CHECKING:  # annotation only; a runtime import of main would be a cycle
    from vahub.cli.main import CliState

console = Console()
err = Console(stderr=True)


def run(
    ctx: typer.Context,
    host: str = typer.Option(None, "--host", help="Override web.host."),
    port: int = typer.Option(None, "--port", help="Override web.port."),
    log_level: str = typer.Option(None, "--log-level", help="Override hub.log_level."),
) -> None:
    """Run the hub in the foreground (this is what a systemd unit calls)."""
    cli: CliState = ctx.obj
    config = _apply_overrides(cli.load(), host=host, port=port, log_level=log_level)
    _warn_about_exposure(config)
    _serve(config, cli.path)


def serve(
    ctx: typer.Context,
    host: str = typer.Option(None, "--host", help="Override web.host."),
    port: int = typer.Option(None, "--port", help="Override web.port."),
    log_level: str = typer.Option("INFO", "--log-level", help="Log level for this session."),
) -> None:
    """Run the hub with human readable logging, for working on it."""
    cli: CliState = ctx.obj
    config = _apply_overrides(cli.load(), host=host, port=port, log_level=log_level, log_format="console")
    _warn_about_exposure(config)
    console.print(f"vahub on [bold]http://{config.web.host}:{config.web.port}[/bold]  (ctrl-c to stop)")
    _serve(config, cli.path)


def start(
    ctx: typer.Context,
    host: str = typer.Option(None, "--host", help="Override web.host."),
    port: int = typer.Option(None, "--port", help="Override web.port."),
) -> None:
    """Start the hub, writing a starter configuration first if none exists.

    This is the one command a first run needs. With no configuration it writes a
    safe default (loopback, login on, mock model, deny policy) and starts; then
    everything else, the owner account, a real model, and modules, is done from
    the web page. With a configuration already present it is just `vahub serve`.
    """
    from vahub.cli.init import default_layout, write_starter_config

    cli: CliState = ctx.obj
    layout = default_layout()
    if cli.config_path is not None:
        layout.config_path = cli.config_path
        layout.secrets_path = cli.config_path.parent / ".env"

    if not layout.config_path.is_file():
        write_starter_config(layout)
        console.print(f"[green]wrote[/green] {layout.config_path} (a starter configuration)")

    config = _apply_overrides(load_config(layout.config_path), host=host, port=port, log_format="console")
    _warn_about_exposure(config)
    console.print(
        f"vahub on [bold]http://{config.web.host}:{config.web.port}[/bold] "
        "(open it and create your account)  (ctrl-c to stop)"
    )
    _serve(config, layout.config_path)


# --------------------------------------------------------------------------
def _apply_overrides(
    config: Config,
    *,
    host: str | None = None,
    port: int | None = None,
    log_level: str | None = None,
    log_format: str | None = None,
) -> Config:
    """Re-validate the config with the command line overrides applied, so that a
    rejected value (port 0, an unknown log level) fails here and not at first use."""
    overrides = {
        ("web", "host"): host,
        ("web", "port"): port,
        ("hub", "log_level"): log_level,
        ("hub", "log_format"): log_format,
    }
    if all(value is None for value in overrides.values()):
        return config

    data = config.model_dump(by_alias=True)
    for (section, key), value in overrides.items():
        if value is not None:
            data[section][key] = value
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"a command line override is not valid:\n  {e}") from e


def _warn_about_exposure(config: Config) -> None:
    """Starting is not the moment to be quiet about an unauthenticated bind."""
    for check in exposure_checks(config):
        if check.status == "fail":
            err.print(f"[red]warning:[/red] {check.detail}. {check.hint}")
        elif check.status == "warn" and check.name == "bind address":
            err.print(f"[yellow]note:[/yellow] {check.detail} is not loopback. {check.hint}")


def _serve(config: Config, config_path: Path) -> None:
    """Start the runtime. The config path is passed on because a manifest's
    {config} placeholder resolves against the directory the file lives in."""
    _configure_logging(config)
    try:
        from vahub.core.runtime import Runtime
    except ImportError as e:
        err.print(f"[red]error:[/red] the hub runtime is not importable: {e}")
        err.print("Install the hub with its dependencies (pip install vahub).")
        raise typer.Exit(1) from e

    try:
        asyncio.run(_run_until_signalled(Runtime(config, config_path)))
    except KeyboardInterrupt:  # ctrl-c between the signal handler and the runtime's own
        raise typer.Exit(130) from None


async def _run_until_signalled(runtime: Any) -> None:
    task = asyncio.create_task(runtime.run(), name="runtime")
    loop = asyncio.get_running_loop()
    stop = getattr(runtime, "request_stop", task.cancel)
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop)
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _configure_logging(config: Config) -> None:
    """Logging belongs to the core; this only passes the configured values on."""
    from vahub.core.logging import configure

    configure(config.hub.log_level, log_format=config.hub.log_format)
