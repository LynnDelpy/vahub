"""`vahub module`: driving the installer, the registry and the contract test kit
from a terminal.

The work lives in vahub.modules; this file is presentation and argument
handling. It also holds the few helpers the wizard and the doctor share, so
there is one answer to "what is installed" rather than three.

Anything that came out of a manifest, a registry index or a module's own stderr
is wrapped in rich.text.Text before it is printed. A description containing
"[red]" is a description, and module output is never markup.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from vahub.config.models import Config
from vahub.contracts.manifest import Manifest
from vahub.modules.installer import Installer, InstallError, InstallResult
from vahub.modules.registry_client import RegistryClient, RegistryError
from vahub.modules.store import InstalledModule, ModuleStore, StoreError, describe_source
from vahub.modules.verify import VerifyError, VerifyReport, verify_installed_sync, verify_source_sync

if TYPE_CHECKING:  # imported for the annotation only; importing it at runtime would be a cycle
    from vahub.cli.main import CliState

app = typer.Typer(no_args_is_help=True, help="Install and inspect modules.")
console = Console()
err = Console(stderr=True)


class ModuleError(RuntimeError):
    """A command line problem, as opposed to one the installer reports."""


# Everything below the CLI reports failure by raising with a message written for
# a person. The top level prints those; a traceback would only bury them.
CLI_ERRORS = (ModuleError, InstallError, RegistryError, StoreError, VerifyError)


# --------------------------------------------------------------------------
# shared helpers (also used by init.py and doctor.py)
# --------------------------------------------------------------------------
def state(ctx: typer.Context) -> CliState:
    return ctx.obj


def store_for(config: Config) -> ModuleStore:
    return ModuleStore.from_config(config)


def registry_for(config: Config, url: str | None = None) -> RegistryClient:
    return RegistryClient.from_config(config, url)


def installed_modules(config: Config) -> list[InstalledModule]:
    return store_for(config).list_installed()


def manifests(config: Config) -> dict[str, Manifest]:
    """Name to manifest, skipping the ones that could not be read. Callers that
    have to report a broken manifest use installed_modules() instead."""
    return {m.name: m.manifest for m in installed_modules(config) if m.manifest is not None}


def installer_for(
    config: Config,
    *,
    offline: bool = False,
    allow_root: bool = False,
    quiet: bool = False,
    registry_url: str | None = None,
) -> Installer:
    return Installer(
        config,
        registry=registry_for(config, registry_url),
        store=store_for(config),
        offline=offline,
        allow_root=allow_root,
        on_progress=None if quiet else _progress,
    )


def _progress(message: str) -> None:
    console.print(Text(f"  {message}", style="dim"))


def static_findings(config: Config, module: InstalledModule) -> list[tuple[str, str]]:
    """What can be said about an installed module without spawning it.

    This is what `vahub doctor` uses: `vahub module verify` starts the module
    and talks MCP to it, which is the right thing for a person asking about one
    module and the wrong thing for a health check over all of them.
    """
    findings: list[tuple[str, str]] = []
    if module.manifest_error:
        findings.append(("fail", f"manifest is unreadable: {module.manifest_error}"))
        return findings
    if module.manifest is None:  # pragma: no cover - manifest_error covers this
        return [("fail", "no manifest")]

    if not module.module_dir.is_dir():
        findings.append(("fail", f"{module.module_dir} is missing; reinstall the module"))
    elif module.record is not None and module.record.has_venv and not module.venv.is_dir():
        findings.append(("fail", f"virtualenv {module.venv} is missing; reinstall the module"))
    if module.record is None:
        findings.append(("warn", "no install record; the hub can run it but not upgrade it"))

    missing = module.missing_config()
    if missing:
        findings.append(("fail", f"required config not set: {', '.join(missing)}"))

    if config.policy.default != "allow":
        uncovered = sorted(
            tool for tool in module.manifest.tools if f"{module.name}.{tool}" not in config.policy.rules
        )
        if uncovered:
            findings.append(("warn", f"denied by policy (no rule): {', '.join(uncovered)}"))
    return findings


def secrets_hint(config_path: Path) -> Path:
    """Where `vahub init` puts credentials, so other commands can point at it."""
    return config_path.parent / ".env"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
@app.command("list")
def list_installed(ctx: typer.Context) -> None:
    """List installed modules."""
    config = state(ctx).load()
    modules = installed_modules(config)
    if not modules:
        console.print(f"No modules installed in {config.hub.modules_dir}.")
        console.print("Browse what is available with [bold]vahub module search[/bold].")
        return

    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("Module")
    table.add_column("Version")
    table.add_column("Tools", justify="right")
    table.add_column("Config")
    table.add_column("Source")
    for module in modules:
        if module.manifest is None:
            table.add_row(
                Text(module.name), Text(module.version), "-",
                Text("unreadable manifest", style="red"), Text(module.source_label),
            )
            continue
        missing = module.missing_config()
        table.add_row(
            Text(module.name),
            Text(module.version),
            str(len(module.manifest.tools)),
            Text("ok", style="green") if not missing
            else Text("missing " + ", ".join(missing), style="yellow"),
            Text(module.source_label),
        )
    console.print(table)

    for module in modules:
        if module.manifest_error:
            err.print(f"[red]{module.name}[/red] ", Text(module.manifest_error))


@app.command()
def search(
    ctx: typer.Context,
    query: str = typer.Argument("", help="Substring to look for in names, tags and descriptions."),
    registry_url: str = typer.Option(None, "--registry", help="Registry index URL or file."),
    offline: bool = typer.Option(False, "--offline", help="Use the cached index only."),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the cached index."),
) -> None:
    """Search the module registry."""
    config = state(ctx).load()
    registry = registry_for(config, registry_url)
    registry.load(refresh=refresh, offline=offline)
    hits = registry.search(query, offline=offline)
    _report_registry(registry)

    if not hits:
        console.print(f"Nothing matches {query!r} in {registry.url}.")
        raise typer.Exit(1)

    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("Module")
    table.add_column("Latest")
    table.add_column("Tags")
    table.add_column("Description")
    known = set(manifests(config))
    for name, entry in hits:
        label = Text(name) if name not in known else Text(f"{name} (installed)", style="green")
        table.add_row(label, Text(entry.latest or ""), Text(", ".join(entry.tags)), Text(entry.description))
    console.print(table)


@app.command()
def info(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Module name."),
    registry_url: str = typer.Option(None, "--registry", help="Registry index URL or file."),
    offline: bool = typer.Option(False, "--offline", help="Do not contact the registry."),
) -> None:
    """Show what a module is, installed or not."""
    cli = state(ctx)
    config = cli.load()
    module = store_for(config).get(name)

    if module is not None:
        _show_installed(config, module)
        return
    if offline:
        raise ModuleError(f"{name} is not installed, and --offline was given")

    registry = registry_for(config, registry_url)
    version, spec = registry.resolve(name, offline=offline)
    entry = registry.entry(name, offline=offline)
    _report_registry(registry)

    console.print(Text(f"{name} {version}", style="bold"), Text("(not installed)", style="dim"))
    if entry.description:
        console.print(Text(entry.description))
    if entry.homepage:
        console.print(Text(entry.homepage, style="dim"))
    console.print("source:   ", Text(describe_source(spec.source)))
    console.print("config:   ", Text(", ".join(spec.requires_config) or "none required"))
    if spec.optional_config:
        console.print("optional: ", Text(", ".join(spec.optional_config)))
    if spec.notes:
        console.print(Text(spec.notes))
    console.print(f"\nInstall it with: vahub module add {name}")


def _show_installed(config: Config, module: InstalledModule) -> None:
    console.print(Text(f"{module.name} {module.version}", style="bold"))
    if module.manifest is not None and module.manifest.description:
        console.print(Text(module.manifest.description))
    if module.manifest is not None and module.manifest.homepage:
        console.print(Text(module.manifest.homepage, style="dim"))
    console.print()
    console.print("source:    ", Text(module.source_label))
    console.print("installed: ", Text(module.installed_at_iso or "unknown"))
    console.print("manifest:  ", Text(str(module.manifest_path)))
    console.print("venv:      ", Text(str(module.venv)))

    if module.manifest is None:
        err.print("[red]the manifest could not be read:[/red] ", Text(module.manifest_error or ""))
        return

    required = module.manifest.config.required
    console.print("config:    ", Text(", ".join(required) or "none required"))
    if module.manifest.config.optional:
        console.print("optional:  ", Text(", ".join(module.manifest.config.optional)))

    for level, message in static_findings(config, module):
        colour = "red" if level == "fail" else "yellow"
        console.print(f"[{colour}]{level}[/{colour}] ", Text(message))

    if module.manifest.tools:
        table = Table(box=box.SIMPLE, header_style="bold")
        table.add_column("Tool")
        table.add_column("Class")
        table.add_column("Policy")
        for tool, spec in sorted(module.manifest.tools.items()):
            allowed = f"{module.name}.{tool}" in config.policy.rules or config.policy.default == "allow"
            table.add_row(
                Text(tool), Text(spec.cls),
                Text("rule present", style="green") if allowed else Text("no rule", style="yellow"),
            )
        console.print(table)


@app.command()
def add(
    ctx: typer.Context,
    name: str = typer.Argument(None, help="Module name in the registry."),
    source: str = typer.Option(
        None, "--source",
        help="Install from here instead of the registry: git+https://host/repo@tag, pypi:pkg==1.0, ./path",
    ),
    version: str = typer.Option(None, "--version", help="Registry version to install."),
    all_modules: bool = typer.Option(False, "--all", help="Install every module in the registry."),
    registry_url: str = typer.Option(None, "--registry", help="Registry index URL or file."),
    force: bool = typer.Option(False, "--force", help="Reinstall even if nothing would change."),
    offline: bool = typer.Option(False, "--offline", help="Use the cached index only."),
    allow_root: bool = typer.Option(False, "--allow-root", help="Permit installing as root."),
) -> None:
    """Install a module, or every module in the registry with --all."""
    cli = state(ctx)
    config = cli.load()
    if all_modules:
        _install_all(
            config, cli.path, force=force, offline=offline,
            allow_root=allow_root, registry_url=registry_url,
        )
        return
    if not name and not source:
        raise ModuleError("give a module name, a --source to install from, or --all")

    console.print(f"Installing {name or source} ...")
    installer = installer_for(config, offline=offline, allow_root=allow_root)
    result = installer.install(name, version=version, source_spec=source, force=force)
    _report_install(result, config, cli.path)


def _install_all(
    config: Config,
    config_path: Path,
    *,
    force: bool,
    offline: bool,
    allow_root: bool,
    registry_url: str | None,
) -> None:
    """Install every module the registry lists. One module failing does not stop
    the rest: each result is reported and a summary follows, and the command
    exits non-zero if any failed."""
    registry = registry_for(config, registry_url)
    index = registry.load(offline=offline)
    _report_registry(registry)
    names = sorted(index.modules)
    if not names:
        console.print(f"The registry at {registry.url} lists no modules.")
        return

    store = ModuleStore.from_config(config)
    installer = installer_for(
        config, offline=offline, allow_root=allow_root, registry_url=registry_url
    )
    installed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for name in names:
        if store.is_installed(name) and not force:
            skipped.append(name)
            console.print(f"[dim]skip[/dim] {name} (already installed; use --force to reinstall)")
            continue
        console.print(f"Installing {name} ...")
        try:
            installer.install(name, force=force)
            installed.append(name)
        except CLI_ERRORS as e:
            failed.append(name)
            # The message may quote a module or a registry, so print it as text.
            err.print(f"[red]failed[/red] {name}: ", Text(str(e)), sep="")

    console.print(
        f"\n[green]{len(installed)} installed[/green], {len(skipped)} skipped, "
        f"{len(failed)} failed."
    )
    if installed:
        console.print(
            f"Add policy rules for the new tools in {config_path}, then `vahub run`."
        )
    if failed:
        raise typer.Exit(1)


@app.command()
def remove(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Module name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Remove an installed module, its virtualenv and its manifest."""
    cli = state(ctx)
    config = cli.load()
    module = store_for(config).get(name)
    if module is None:
        raise ModuleError(f"{name} is not installed in {config.hub.modules_dir}")
    question = f"Remove {name} {module.version} and everything under {module.module_dir}?"
    if not yes and not typer.confirm(question):
        raise typer.Exit(1)

    removed = installer_for(config, quiet=True).remove(name)
    for path in removed:
        console.print("[green]removed[/green] ", Text(str(path)), sep="")

    stale = [key for key in config.policy.rules if key.startswith(f"{name}.")]
    if stale:
        console.print(
            f"{len(stale)} policy rule(s) for {name} are still in {cli.path}; "
            "they match nothing now, but they are worth deleting."
        )


@app.command()
def upgrade(
    ctx: typer.Context,
    name: str = typer.Argument(None, help="Module to upgrade. Omit to upgrade every installed module."),
    version: str = typer.Option(None, "--version", help="Install this version instead of the newest."),
    offline: bool = typer.Option(False, "--offline", help="Use the cached index only."),
    allow_root: bool = typer.Option(False, "--allow-root", help="Permit installing as root."),
) -> None:
    """Reinstall modules at the newest published version."""
    cli = state(ctx)
    config = cli.load()
    store = store_for(config)
    if name is not None and not store.is_installed(name):
        raise ModuleError(f"{name} is not installed in {config.hub.modules_dir}")
    targets = [name] if name else [m.name for m in store.list_installed()]
    if not targets:
        console.print("Nothing installed, nothing to upgrade.")
        return
    if version is not None and len(targets) > 1:
        raise ModuleError("--version applies to one module; name the module to upgrade")

    installer = installer_for(config, offline=offline, allow_root=allow_root)
    failed = 0
    for target in targets:
        console.print(f"\nUpgrading {target} ...")
        try:
            _report_install(installer.upgrade(target, version=version), config, cli.path)
        except (InstallError, RegistryError) as e:
            # One module that cannot be upgraded must not stop the others.
            failed += 1
            err.print(f"[red]{target}:[/red] ", Text(str(e)), sep="")
    if failed:
        raise typer.Exit(1)


@app.command()
def verify(
    ctx: typer.Context,
    name: str = typer.Argument(None, help="Installed module to verify."),
    source: Path = typer.Option(None, "--source", help="Verify a module source directory instead."),
    timeout: float = typer.Option(None, "--timeout", help="Seconds to wait for the handshake."),
) -> None:
    """Start a module and check it against the module contract.

    This spawns the module the way the hub does and talks MCP to it, so it needs
    the module's own configuration in the environment.
    """
    if source is not None:
        # A source tree is verified on its own terms: this is what a module
        # author runs in CI, where there is no hub configuration at all.
        report = verify_source_sync(source, startup_timeout_s=timeout)
        _print_report(report)
        raise typer.Exit(0 if report.ok else 1)

    config = state(ctx).load()
    names = [name] if name else [m.name for m in installed_modules(config)]
    if not names:
        console.print(f"No modules installed in {config.hub.modules_dir}.")
        return

    failed = 0
    for target in names:
        report = verify_installed_sync(target, config, startup_timeout_s=timeout)
        _print_report(report)
        failed += 0 if report.ok else 1
    if failed:
        raise typer.Exit(1)


def _print_report(report: VerifyReport) -> None:
    mark = "[green]ok[/green]" if report.ok else "[red]failed[/red]"
    console.print(f"\n{mark} {report.module}")
    # The report quotes the module's own output, so it is printed as text.
    console.print(Text(report.text()))


# --------------------------------------------------------------------------
def _report_registry(registry: RegistryClient) -> None:
    for warning in registry.warnings:
        err.print("[yellow]note:[/yellow] ", Text(warning), sep="")


def outcome(result: InstallResult) -> str:
    """One line saying what actually happened, which is not always "installed"."""
    if not result.changed:
        return f"up to date: {result.name} {result.version}, nothing to do"
    if result.previous_version == result.version:
        return f"reinstalled {result.name} {result.version}"
    if result.upgraded:
        return f"upgraded {result.name} {result.previous_version} -> {result.version}"
    return f"installed {result.name} {result.version}"


def _report_install(result: InstallResult, config: Config, config_path: Path) -> None:
    console.print(f"[green]{outcome(result)}[/green]")
    for warning in result.warnings:
        err.print("[yellow]note:[/yellow] ", Text(warning), sep="")

    if result.missing_config:
        secrets = secrets_hint(config_path)
        console.print()
        console.print(
            f"{result.name} cannot start until these are set: {', '.join(result.missing_config)}"
        )
        console.print(f"Put them in {secrets} (one KEY=value per line, mode 600) and restart the hub.")

    # Only the tools the gate would currently refuse are worth mentioning.
    if config.policy.default != "allow":
        uncovered = sorted(
            f"{result.name}.{tool}" for tool in result.manifest.tools
            if f"{result.name}.{tool}" not in config.policy.rules
        )
        if uncovered:
            console.print(
                f"No policy rule yet for {', '.join(uncovered)}; the agent cannot call them until "
                f"you add one in {config_path}."
            )
