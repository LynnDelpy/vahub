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
    # The filters are applied in the query, before the limit, so `--denied -n 50`
    # returns the 50 most recent denials rather than the denials that happen to
    # be among the 50 most recent calls.
    rows = asyncio.run(_read(config, limit, principal=principal or None, decision="deny" if denied else None))

    if as_json:
        # Machine-readable output: plain, uncoloured JSON. Rich's print_json
        # would syntax-highlight it, and under FORCE_COLOR (or any terminal)
        # the ANSI codes make it unparseable to whatever is consuming it.
        print(json.dumps(rows, default=str, indent=2))
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


async def _read(
    config: Any, limit: int, *, principal: str | None = None, decision: str | None = None
) -> list[dict[str, Any]]:
    from vahub.storage.store import Store

    store = Store(config.hub.db_path)
    await store.open()
    try:
        return await store.recent_tool_calls(limit=limit, principal=principal, decision=decision)
    finally:
        await store.close()
