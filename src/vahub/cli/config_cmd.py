"""`vahub config`: look at the effective configuration without opening the file.

What is printed is the config after interpolation and after the VAHUB_*
environment overrides, which is the only version that matters at runtime and the
one nobody can reconstruct by reading the YAML.

Every value whose key looks like a credential is replaced before printing. The
test is on the key, not the value, so a secret is redacted even when the loader
resolved it from somewhere this command knows nothing about.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import typer
import yaml
from rich.console import Console
from rich.syntax import Syntax

from vahub.config.models import ConfigError

if TYPE_CHECKING:  # annotation only; a runtime import of main would be a cycle
    from vahub.cli.main import CliState

app = typer.Typer(no_args_is_help=True, help="Inspect the effective configuration.")
console = Console()
err = Console(stderr=True)

# Anchored at a word boundary so `max_tokens` and `tokens_per_turn` stay
# readable while `api_key` and `access_token` do not.
SECRET_KEY = re.compile(r"(^|_)(key|token|secret|password|passwd|credential)$", re.IGNORECASE)
REDACTED = "***redacted***"


def _redact(data: Any, key: str | None = None) -> Any:
    if isinstance(data, dict):
        return {k: _redact(v, k) for k, v in data.items()}
    if isinstance(data, list):
        return [_redact(v) for v in data]
    if key is not None and SECRET_KEY.search(key):
        return None if data is None else REDACTED
    return data


@app.command("show")
def show(
    ctx: typer.Context,
    output: str = typer.Option("yaml", "--format", "-f", help="yaml or json."),
) -> None:
    """Print the effective configuration with credentials redacted."""
    cli: CliState = ctx.obj
    config = cli.load()
    data = _redact(json.loads(config.model_dump_json(by_alias=True)))

    if output == "json":
        # Machine-readable output: plain, uncoloured JSON. Rich's print_json
        # would syntax-highlight it, and under FORCE_COLOR (or any terminal)
        # the ANSI codes make it unparseable to whatever is consuming it.
        print(json.dumps(data, indent=2))
        return
    if output != "yaml":
        err.print(f"[red]error:[/red] unknown format {output!r}; use yaml or json")
        raise typer.Exit(2)

    text = yaml.safe_dump(data, sort_keys=False, width=100, default_flow_style=False)
    console.print(Syntax(text, "yaml", background_color="default", word_wrap=True))
    console.print(f"[dim]source: {cli.path}, credentials shown as {REDACTED}[/dim]")


@app.command("path")
def path(ctx: typer.Context) -> None:
    """Print the path of the configuration file that would be read."""
    cli: CliState = ctx.obj
    # Plain print, no markup and no decoration: this output gets used in scripts.
    print(cli.path)
    if not cli.path.is_file():
        err.print(f"[yellow]note:[/yellow] {cli.path} does not exist; `vahub init` creates it")
        raise typer.Exit(1)


@app.command("validate")
def validate(ctx: typer.Context) -> None:
    """Load and validate the configuration, then say what it resolved to."""
    cli: CliState = ctx.obj
    try:
        config = cli.load()
    except ConfigError as e:
        err.print(f"[red]invalid[/red] {cli.path}")
        err.print(str(e))
        raise typer.Exit(1) from e

    console.print(f"[green]valid[/green] {cli.path}")
    console.print(
        f"  bind {config.web.host}:{config.web.port}, "
        f"llm {config.llm.provider}/{config.llm.model}, "
        f"{len(config.policy.rules)} policy rules, {len(config.schedules)} schedules"
    )
