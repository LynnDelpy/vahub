"""`vahub user`: manage the accounts that can log in.

The hub never invents a credential. These commands are how a person creates
their own account and sets their own password: the password is read from a
prompt, hashed with scrypt, and only the hash is ever stored. There is no way to
print or recover a password, only to replace it.

An account holds a role. An `admin` may do everything the web interface offers,
including installing an app and managing accounts; a `user` may talk to the
assistant, arrange the dashboard, approve a held-back action, and edit places
and schedules. The first account is an admin (somebody has to be able to make
the second one); `vahub user add` makes a plain user unless you pass `--admin`.

These commands stay the way back in when nobody can sign in: they run on the
host, against the database, and answer to no session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.table import Table

from vahub.auth import MIN_PASSWORD_LEN, ROLE_ADMIN, ROLE_USER, ROLES, USERNAME_RE, hash_password

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
    admin: bool = typer.Option(
        False, "--admin", help="Give this account the admin role (apps and accounts)."
    ),
) -> None:
    """Create an account. Prompts for the password, twice.

    Without --admin the account is a plain user: it can use the assistant but
    cannot install an app or manage accounts. The very first account on a hub is
    an admin whatever you pass, because otherwise nobody could make the second.
    """
    if not USERNAME_RE.match(username):
        err.print("[red]username must be 2-32 chars: a lowercase letter or digit, then a-z 0-9 . _ -[/red]")
        raise typer.Exit(1)
    config = _config(ctx)
    hashed = hash_password(_prompt_password())
    role = ROLE_ADMIN if admin else ROLE_USER
    granted: list[str] = []

    async def go(store: Store) -> None:
        if await store.get_user(username) is not None:
            err.print(f"[red]account {username!r} already exists[/red]")
            raise typer.Exit(1)
        # The first account is always an admin: a hub whose only account cannot
        # add a second one, or configure an app, is a hub nobody can finish
        # setting up.
        first = await store.count_users() == 0
        granted.append(ROLE_ADMIN if (first or admin) else ROLE_USER)
        await store.create_user(username, hashed, display_name, role=granted[0])

    asyncio.run(_with_store(config, go))
    console.print(f"[green]created[/green] {granted[0]} account {username!r}")
    if granted[0] == ROLE_ADMIN and not admin:
        console.print("[dim]the first account on a hub is an admin, so this one is.[/dim]")
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
    for column in ("username", "name", "role", "created", "status"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row.get("username", "")),
            str(row.get("display_name") or ""),
            str(row.get("role") or ROLE_USER),
            _when(row.get("created_at")),
            "disabled" if row.get("disabled") else "active",
        )
    console.print(table)


@app.command("role")
def role(
    ctx: typer.Context,
    username: str = typer.Argument(..., help="Whose role to change."),
    role: str = typer.Argument(..., help=f"One of: {', '.join(ROLES)}."),
) -> None:
    """Make an account an admin, or take that back.

    The web interface refuses the change that would leave no admin able to sign
    in, because from a browser there would be no way back. Here it only warns:
    this command is the way back, and an operator standing on the host must be
    able to take a hub apart and put it together again.
    """
    if role not in ROLES:
        err.print(f"[red]role must be one of: {', '.join(ROLES)}[/red]")
        raise typer.Exit(1)
    config = _config(ctx)

    async def go(store: Store) -> None:
        if await store.get_user(username) is None:
            err.print(f"[red]no such account {username!r}[/red]")
            raise typer.Exit(1)
        if role != ROLE_ADMIN and await store.count_admins(excluding=username) == 0:
            _warn_last_admin(username)
        await store.set_user_role(username, role)

    asyncio.run(_with_store(config, go))
    console.print(f"[green]{username!r}[/green] is now {role}")


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
        if disabled and await store.count_admins(excluding=username) == 0:
            _warn_last_admin(username)
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

    async def go(store: Store) -> bool:
        if await store.get_user(username) and await store.count_admins(excluding=username) == 0:
            _warn_last_admin(username)
        return await store.delete_user(username)

    removed = asyncio.run(_with_store(config, go))
    if not removed:
        err.print(f"[red]no such account {username!r}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]removed[/green] {username!r}")


def _warn_last_admin(username: str) -> None:
    """Say plainly that the web interface is about to have no administrator.

    The CLI does not refuse this. It runs on the host, against the database,
    with no session involved, so it is exactly the tool that undoes the mistake
    (`vahub user role <name> admin`). The web routes do refuse it, because a
    browser has no such second door."""
    err.print(
        f"[yellow]warning:[/yellow] {username!r} was the only admin who could sign in. "
        "Nobody can now install an app or manage accounts from the web. "
        "Give somebody the role back with `vahub user role <name> admin`."
    )


def _when(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
