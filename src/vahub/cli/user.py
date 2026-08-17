"""`vahub user`: manage the accounts that can log in.

The hub never invents a credential. These commands are how a person creates
their own account and sets their own password: the password is read from a
prompt, hashed with scrypt, and only the hash is ever stored. There is no way to
print or recover a password, only to replace it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.table import Table

from vahub.auth import MIN_PASSWORD_LEN, USERNAME_RE, hash_password

if TYPE_CHECKING:
    from vahub.config.models import Config
    from vahub.storage.store import Store

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Accounts that can log in to the web interface.",
)
console = Console()
err = Console(stderr=True)


def _config(ctx: typer.Context) -> Config:
    return ctx.obj.load()


async def _with_store[T](config: Config, fn: Callable[[Store], Awaitable[T]]) -> T:
    from vahub.storage.store import Store

    store = Store(config.hub.db_path)
    await store.open()
    try:
        return await fn(store)
    finally:
        await store.close()


def _prompt_password() -> str:
    password = typer.prompt("password", hide_input=True, confirmation_prompt=True)
    if len(password) < MIN_PASSWORD_LEN:
        err.print(f"[red]password must be at least {MIN_PASSWORD_LEN} characters[/red]")
        raise typer.Exit(1)
    return password


@app.command("add")
def add(
    ctx: typer.Context,
    username: str = typer.Argument(..., help="Login name (lowercase, 2-32 chars)."),
    display_name: str = typer.Option(None, "--name", help="A friendlier name for the UI."),
) -> None:
    """Create an account. Prompts for the password, twice."""
    if not USERNAME_RE.match(username):
        err.print("[red]username must be 2-32 chars: a lowercase letter or digit, then a-z 0-9 . _ -[/red]")
        raise typer.Exit(1)
    config = _config(ctx)
    hashed = hash_password(_prompt_password())

    async def go(store: Store) -> None:
        if await store.get_user(username) is not None:
            err.print(f"[red]account {username!r} already exists[/red]")
            raise typer.Exit(1)
        await store.create_user(username, hashed, display_name)

    asyncio.run(_with_store(config, go))
    console.print(f"[green]created[/green] account {username!r}")
    if not config.web.auth.enabled:
        console.print(
            "[yellow]note:[/yellow] web.auth.enabled is false, so login is not enforced yet. "
            "Set it true to require sign in."
        )


@app.command("passwd")
def passwd(
    ctx: typer.Context,
    username: str = typer.Argument(..., help="Whose password to change."),
) -> None:
    """Set a new password for an existing account. Logs the account out of every
    session, so a leaked cookie stops working."""
    config = _config(ctx)
    hashed = hash_password(_prompt_password())

    async def go(store: Store) -> None:
        if not await store.set_password(username, hashed):
            err.print(f"[red]no such account {username!r}[/red]")
            raise typer.Exit(1)
        # A password change ends existing sessions, so a leaked cookie dies.
        await store.drop_user_sessions(username)

    asyncio.run(_with_store(config, go))
    console.print(f"[green]updated[/green] password for {username!r}")


@app.command("list")
def list_users(ctx: typer.Context) -> None:
    """Show the accounts."""
    config = _config(ctx)
    rows = asyncio.run(_with_store(config, lambda s: s.list_users()))
    if not rows:
        console.print("[dim]no accounts yet. Create one with `vahub user add <name>`.[/dim]")
        return
    table = Table(box=None, pad_edge=False, header_style="dim")
    for column in ("username", "name", "created", "status"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row.get("username", "")),
            str(row.get("display_name") or ""),
            _when(row.get("created_at")),
            "disabled" if row.get("disabled") else "active",
        )
    console.print(table)


@app.command("disable")
def disable(ctx: typer.Context, username: str = typer.Argument(...)) -> None:
    """Block an account from logging in, without deleting it. Ends its sessions."""
    _set_disabled(ctx, username, True)


@app.command("enable")
def enable(ctx: typer.Context, username: str = typer.Argument(...)) -> None:
    """Allow a disabled account to log in again."""
    _set_disabled(ctx, username, False)


def _set_disabled(ctx: typer.Context, username: str, disabled: bool) -> None:
    config = _config(ctx)

    async def go(store: Store) -> None:
        if not await store.set_user_disabled(username, disabled):
            err.print(f"[red]no such account {username!r}[/red]")
            raise typer.Exit(1)
        if disabled:
            await store.drop_user_sessions(username)

    asyncio.run(_with_store(config, go))
    console.print(f"[green]{'disabled' if disabled else 'enabled'}[/green] {username!r}")


@app.command("remove")
def remove(
    ctx: typer.Context,
    username: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Delete an account and its sessions."""
    if not yes and not typer.confirm(f"delete account {username!r}?"):
        raise typer.Exit(1)
    config = _config(ctx)
    removed = asyncio.run(_with_store(config, lambda s: s.delete_user(username)))
    if not removed:
        err.print(f"[red]no such account {username!r}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]removed[/green] {username!r}")


def _when(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
