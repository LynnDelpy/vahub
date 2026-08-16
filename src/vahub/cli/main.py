"""The `vahub` command.

The global --config option is resolved once and carried on the typer context, so
every subcommand reads the same file and `--config` works in front of any of
them.

A ConfigError is a mistake in a file a human wrote, not a crash: it is printed
as the message it was raised with and the process exits 1. A traceback for a
misspelled key helps nobody at three in the morning.
"""

from __future__ import annotations

import errno
import sys
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.text import Text

from vahub.__about__ import __version__
from vahub.cli import audit as audit_cmd
from vahub.cli import config_cmd, doctor, init, module, run
from vahub.cli import user as user_cmd
from vahub.cli.module import CLI_ERRORS
from vahub.config.loader import default_config_path, load_config
from vahub.config.models import Config, ConfigError

err = Console(stderr=True)


@dataclass
class CliState:
    """What every subcommand needs from the command line itself."""

    config_path: Path | None = None  # None means "search the default locations"

    @property
    def path(self) -> Path:
        return self.config_path or default_config_path()

    def load(self, *, strict_secrets: bool = True) -> Config:
        return load_config(self.config_path, strict_secrets=strict_secrets)


app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="A self-hosted voice assistant hub: an LLM agent with a policy gate in front of every action.",
)
app.add_typer(module.app, name="module")
app.add_typer(config_cmd.app, name="config")
app.add_typer(user_cmd.app, name="user")
app.command("init")(init.init)
app.command("run")(run.run)
app.command("serve")(run.serve)
app.command("doctor")(doctor.doctor)
app.command("audit")(audit_cmd.audit)


def _version(value: bool) -> None:
    if value:
        print(f"vahub {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    ctx: typer.Context,
    config: Path = typer.Option(
        None, "--config", "-c", envvar="VAHUB_CONFIG", metavar="PATH",
        help="Configuration file. Defaults to ./vahub.yaml, then /etc/vahub/vahub.yaml.",
    ),
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Print the version and exit."
    ),
) -> None:
    ctx.obj = CliState(config_path=config)


def _hoist_config_option(argv: list[str]) -> list[str]:
    """Allow --config after the subcommand as well as before it.

    `--config` belongs to the top-level command, so strictly it has to precede
    the subcommand: `vahub --config x doctor`. Everybody writes
    `vahub doctor --config x` instead, and being told "No such option" for a
    flag that plainly exists is a poor first impression. Moving it to the front
    costs a few lines and makes both spellings work.
    """
    out: list[str] = []
    hoisted: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--config", "-c") and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            hoisted = [arg, argv[i + 1]]
            i += 2
            continue
        if arg.startswith("--config="):
            hoisted = [arg]
            i += 1
            continue
        out.append(arg)
        i += 1
    return hoisted + out


def main() -> None:
    """Console entry point. Turns the errors a user can cause into messages."""
    try:
        app(args=_hoist_config_option(sys.argv[1:]))
    except (ConfigError, *CLI_ERRORS) as e:
        # The message is written for a terminal and may quote a file, so it is
        # printed as text rather than parsed as rich markup.
        err.print("[red]error:[/red] ", Text(str(e)), sep="")
        sys.exit(1)
    except OSError as e:
        # A filesystem or permission problem (most often the state directory not
        # being writable) is an environment issue, not a bug: turn it into a
        # message with a way forward instead of a traceback.
        err.print("[red]error:[/red] ", Text(str(e)), sep="")
        if getattr(e, "errno", None) in (errno.EACCES, errno.EPERM):
            err.print(
                "[dim]the hub could not create or write its files. Run `vahub init` first (it picks a "
                "writable location when you are not root), or point hub.state_dir and hub.modules_dir "
                "at a directory you own.[/dim]"
            )
        sys.exit(1)
    except KeyboardInterrupt:
        err.print("\ninterrupted")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
