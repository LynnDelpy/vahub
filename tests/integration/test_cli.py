"""The CLI surface: `vahub` and its subcommands, driven through CliRunner.

These exercise the command wiring, argument handling, and the human-facing
output and exit codes, against a temporary config and state directory. Anything
that would reach the network (the doctor LLM probe) is kept offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vahub.cli.main import app

runner = CliRunner()


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    """A minimal, valid config whose state and module dirs live under tmp."""
    state = tmp_path / "state"
    mods = tmp_path / "modules.d"
    state.mkdir()
    mods.mkdir()
    path = tmp_path / "vahub.yaml"
    path.write_text(
        "hub:\n"
        f"  state_dir: {state}\n"
        f"  modules_dir: {mods}\n"
        "web:\n"
        "  auth: { enabled: true }\n"
        "llm:\n"
        "  provider: mock\n"
        "policy:\n"
        "  default: deny\n"
        "  rules: {}\n"
    )
    return path


def run(cfg: Path, *args: str, **kw):
    return runner.invoke(app, ["--config", str(cfg), *args], **kw)


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------
def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "vahub" in result.stdout


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "policy gate" in result.stdout.lower() or result.exit_code == 0


# --------------------------------------------------------------------------
# user
# --------------------------------------------------------------------------
def test_user_add_list_passwd_disable_remove(cfg: Path) -> None:
    assert "no accounts" in run(cfg, "user", "list").stdout.lower()

    added = run(cfg, "user", "add", "lynn", input="hunter2pass\nhunter2pass\n")
    assert added.exit_code == 0
    assert "created" in added.stdout.lower()

    listing = run(cfg, "user", "list")
    assert "lynn" in listing.stdout and "active" in listing.stdout

    # A second account with the same name is refused.
    dup = run(cfg, "user", "add", "lynn", input="otherpass1\notherpass1\n")
    assert dup.exit_code == 1

    assert run(cfg, "user", "passwd", "lynn", input="newpass1234\nnewpass1234\n").exit_code == 0
    assert run(cfg, "user", "disable", "lynn").exit_code == 0
    assert "disabled" in run(cfg, "user", "list").stdout.lower()
    assert run(cfg, "user", "enable", "lynn").exit_code == 0
    assert run(cfg, "user", "remove", "lynn", "--yes").exit_code == 0
    assert "no accounts" in run(cfg, "user", "list").stdout.lower()


def test_user_add_rejects_a_bad_username(cfg: Path) -> None:
    r = run(cfg, "user", "add", "Bad Name", input="hunter2pass\nhunter2pass\n")
    assert r.exit_code == 1


def test_user_add_rejects_a_short_password(cfg: Path) -> None:
    r = run(cfg, "user", "add", "lynn", input="short\nshort\n")
    assert r.exit_code == 1


def test_user_passwd_of_unknown_account_fails(cfg: Path) -> None:
    r = run(cfg, "user", "passwd", "nobody", input="hunter2pass\nhunter2pass\n")
    assert r.exit_code == 1


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------
def test_audit_on_an_empty_log(cfg: Path) -> None:
    r = run(cfg, "audit")
    assert r.exit_code == 0
    assert "nothing recorded" in r.stdout.lower()


def test_audit_shows_recorded_calls(cfg: Path) -> None:
    _seed_audit(cfg)
    allrows = run(cfg, "audit")
    assert "door.unlock" in allrows.stdout and "time.now" in allrows.stdout

    denied = run(cfg, "audit", "--denied")
    assert "door.unlock" in denied.stdout and "time.now" not in denied.stdout

    as_json = run(cfg, "audit", "--json")
    assert as_json.exit_code == 0 and '"tool": "unlock"' in as_json.stdout


def test_audit_principal_filter(cfg: Path) -> None:
    _seed_audit(cfg)
    r = run(cfg, "audit", "--principal", "scheduler")
    assert "time.now" in r.stdout and "door.unlock" not in r.stdout


def _seed_audit(cfg: Path) -> None:
    import asyncio

    from vahub.config.loader import load_config
    from vahub.storage.store import Store

    async def go() -> None:
        store = Store(load_config(cfg).hub.db_path)
        await store.open()
        await store.record_tool_call("agent", "door", "unlock", {}, "deny", "denied", 1.0)
        await store.record_tool_call("scheduler", "time", "now", {}, "allow", "ok", 1.0)
        await store.close()

    asyncio.run(go())


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def test_config_show(cfg: Path) -> None:
    shown = run(cfg, "config", "show")
    assert shown.exit_code == 0 and "policy" in shown.stdout and "mock" in shown.stdout


def test_config_show_as_json(cfg: Path) -> None:
    import json

    shown = run(cfg, "config", "show", "--format", "json")
    assert shown.exit_code == 0
    data = json.loads(shown.stdout)
    assert data["llm"]["provider"] == "mock"


def test_config_show_redacts_secrets(tmp_path: Path) -> None:
    path = tmp_path / "vahub.yaml"
    path.write_text("llm:\n  provider: mock\n  api_key: super-secret-value\n")
    shown = runner.invoke(app, ["--config", str(path), "config", "show"])
    assert "super-secret-value" not in shown.stdout


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def test_doctor_on_a_valid_config(cfg: Path) -> None:
    r = run(cfg, "doctor", "--offline")
    assert r.exit_code == 0
    assert "policy" in r.stdout.lower() or "configuration" in r.stdout.lower()


def test_doctor_reports_a_fail_open_policy(tmp_path: Path) -> None:
    # A destructive rule the agent can reach without confirming must not even
    # load, so doctor surfaces the config error rather than a clean report.
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "policy:\n"
        "  default: deny\n"
        "  rules:\n"
        "    door.unlock: { class: destructive, constraints: { id: { max_len: 10 } } }\n"
    )
    r = runner.invoke(app, ["--config", str(bad), "doctor", "--offline"])
    assert r.exit_code != 0


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------
def test_init_generates_a_config_that_loads(tmp_path: Path) -> None:
    from vahub.config.loader import load_config

    target = tmp_path / "vahub.yaml"
    r = runner.invoke(app, ["--config", str(target), "init", "-n", "--no-network"])
    assert r.exit_code in (0, 1)  # warnings may exit 1, but the file must exist and load
    assert target.exists()
    load_config(target, strict_secrets=False)  # raises if invalid


# --------------------------------------------------------------------------
# module
# --------------------------------------------------------------------------
def test_module_list_when_empty(cfg: Path) -> None:
    r = run(cfg, "module", "list")
    assert r.exit_code == 0


def test_module_add_from_a_local_source(cfg: Path) -> None:
    src = Path(__file__).resolve().parents[2].parent / "vahub-modules" / "modules" / "time"
    if not src.exists():
        pytest.skip("vahub-modules checkout not present")
    added = run(cfg, "module", "add", "--source", str(src))
    assert added.exit_code == 0
    listing = run(cfg, "module", "list")
    assert "time" in listing.stdout


# --------------------------------------------------------------------------
# error handling
# --------------------------------------------------------------------------
def test_a_malformed_config_is_a_clean_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("web:\n  port: not-a-number\n")
    r = runner.invoke(app, ["--config", str(broken), "doctor", "--offline"])
    assert r.exit_code != 0
