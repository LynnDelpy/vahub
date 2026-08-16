"""`vahub audit`: what the assistant actually did.

Every tool call the hub makes is written to the database, allowed or denied,
with the principal that made it. That record is the answer to the only question
that matters after something unexpected happens at three in the morning: was it
the model, a routine, or a person, and with what arguments.

It is read here rather than over HTTP on purpose. The web surface is the
assistant, and someone who can talk to the assistant should not automatically be
able to read the history of everything it has ever been asked to do. Reading it
requires access to the host, which is the same access needed to change the
policy anyway.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from vahub.cli.main import CliState

console = Console()

# How a decision reads at a glance. Denials and confirmations are the
# interesting rows, so they get the colour.
_DECISION_STYLE = {
    "allow": "dim",
    "deny": "red",
    "confirm": "yellow",
    "allow-confirmed": "green",
}


def audit(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=1000, help="How many rows to show."),
    principal: str = typer.Option(None, "--principal", help="Only this principal (agent, scheduler, ...)."),
    denied: bool = typer.Option(False, "--denied", help="Only calls the policy refused."),
    as_json: bool = typer.Option(False, "--json", help="Emit raw rows for scripting."),
) -> None:
    """Show recent tool calls: who asked, what for, and what the policy decided."""
    cli: CliState = ctx.obj
    config = cli.load()
    rows = asyncio.run(_read(config, limit))

    if principal:
        rows = [r for r in rows if str(r.get("principal", "")) == principal]
    if denied:
        rows = [r for r in rows if str(r.get("decision", "")) == "deny"]

    if as_json:
        console.print_json(json.dumps(rows, default=str))
        return
    if not rows:
        console.print("[dim]nothing recorded yet[/dim]")
        return

    table = Table(box=None, pad_edge=False, header_style="dim")
    for column in ("when", "who", "what", "decision", "result"):
        table.add_column(column)
    for row in rows:
        decision = str(row.get("decision", ""))
        # Arguments were shaped by a module and by the model. Wrapping them in
        # Text stops rich from interpreting anything in them as markup.
        table.add_row(
            _when(row.get("ts")),
            Text(str(row.get("principal", ""))),
            Text(f"{row.get('module', '')}.{row.get('tool', '')}"),
            Text(decision, style=_DECISION_STYLE.get(decision, "")),
            Text(str(row.get("result", ""))),
        )
    console.print(table)

    refused = sum(1 for r in rows if r.get("decision") == "deny")
    if refused:
        console.print(f"\n[dim]{refused} of {len(rows)} shown were refused by the policy.[/dim]")


def _when(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(ts or "")


async def _read(config: Any, limit: int) -> list[dict[str, Any]]:
    from vahub.storage.store import Store

    store = Store(config.hub.db_path)
    await store.open()
    try:
        return await store.recent_tool_calls(limit=limit)
    finally:
        await store.close()
