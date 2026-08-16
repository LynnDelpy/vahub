"""The module manifest: what a module may declare about itself.

The manifest is written by `vahub module add` but is also the thing a
third-party module author hands you, so it is treated as untrusted input: a
name that is not a name, a tool that squats on a reserved one, or a command
that is a shell string all have to be rejected before anything is spawned.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from vahub.contracts.manifest import (
    HEALTH_TOOL,
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    as_json_schema,
    load_manifests,
    manifest_errors,
)

MINIMAL = {"name": "demo", "runtime": {"command": ["/usr/bin/true"]}}


def manifest(**overrides) -> dict:
    data = dict(MINIMAL)
    data.update(overrides)
    return data


def test_minimal_manifest_has_usable_defaults() -> None:
    m = Manifest.model_validate(MINIMAL)
    assert m.schema_version == MANIFEST_SCHEMA_VERSION
    assert m.version == "0.0.0"
    assert m.tools == {}
    assert m.config.required == []
    assert m.health.interval_s > 0
    assert m.restart.max_retries >= 0


def test_full_manifest_round_trips_through_yaml(tmp_path: Path) -> None:
    source = {
        "name": "homeassistant",
        "version": "0.2.0",
        "description": "Lights, locks and sensors",
        "runtime": {"command": ["{venv}/bin/python", "-m", "mod"], "user": "vahub-mod-ha",
            "cwd": "{state}/ha"},
        "config": {"required": ["HA_URL", "HA_TOKEN"], "optional": ["HA_VERIFY_SSL"]},
        "audit": {"redact": ["HA_TOKEN"]},
        "tools": {"light_turn_on": {"class": "write"}, "lock_unlock": {"class": "destructive"}},
    }
    path = tmp_path / "ha.yaml"
    path.write_text(Manifest.model_validate(source).to_yaml())
    reloaded = Manifest.from_file(path)
    assert reloaded.tools["lock_unlock"].cls == "destructive"
    assert reloaded.config.required == ["HA_URL", "HA_TOKEN"]
    assert reloaded.audit.redact == ["HA_TOKEN"]
    # `class` is a Python keyword; the YAML on disk must still say `class`.
    assert yaml.safe_load(path.read_text())["tools"]["light_turn_on"]["class"] == "write"


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["home-assistant", "ha2", "a", "x_y-z9", "a" * 64])
def test_valid_module_names(name: str) -> None:
    assert Manifest.model_validate(manifest(name=name)).name == name


@pytest.mark.parametrize(
    "name",
    ["", "Home", "2fast", "-leading", "_leading", "has space", "dot.ted", "a" * 65, "üü"],
)
def test_invalid_module_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest(name=name))


@pytest.mark.parametrize("tool", ["light_turn_on", "get_state", "_private", "A", "x" * 64])
def test_valid_tool_names(tool: str) -> None:
    m = Manifest.model_validate(manifest(tools={tool: {"class": "read"}}))
    assert tool in m.tools


@pytest.mark.parametrize("tool", ["light-turn-on", "light.turn.on", "9lives", "has space", "x" * 65])
def test_invalid_tool_names_are_rejected(tool: str) -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest(tools={tool: {"class": "read"}}))


def test_health_tool_cannot_be_declared() -> None:
    # A module that could declare __health could impersonate the hub's own probe.
    with pytest.raises(ValidationError, match="reserved"):
        Manifest.model_validate(manifest(tools={HEALTH_TOOL: {"class": "read"}}))


def test_any_dunder_tool_is_reserved() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        Manifest.model_validate(manifest(tools={"__secret": {"class": "read"}}))


def test_unknown_tool_class_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest(tools={"t": {"class": "admin"}}))


# --------------------------------------------------------------------------
# runtime
# --------------------------------------------------------------------------
def test_command_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest(runtime={"command": []}))


def test_command_must_be_a_list_not_a_shell_string() -> None:
    # "sh -c ..." as one string is exactly what an injected argument escapes into.
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest(runtime={"command": "python -m mod && rm -rf /"}))


def test_command_elements_must_be_strings() -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest(runtime={"command": ["python", 7]}))


def test_unknown_manifest_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest(privileged=True))


def test_unknown_runtime_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest(runtime={"command": ["/bin/true"], "shell": True}))


def test_backoff_base_must_grow() -> None:
    # A base of 1 makes the backoff a constant, so a crash loop stays a tight loop.
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest(restart={"backoff_base_s": 1.0}))


# --------------------------------------------------------------------------
# placeholder expansion
# --------------------------------------------------------------------------
def test_expand_resolves_placeholders(tmp_path: Path) -> None:
    m = Manifest.model_validate(
        manifest(
            runtime={
                "command": ["{venv}/bin/python", "-m", "mod", "--conf={config}/mod.yaml"],
                "cwd": "{state}/modules/demo",
                "pythonpath": "{state}/src",
            }
        )
    )
    expanded = m.expand(venv=tmp_path / "venv", state=tmp_path / "state", config=tmp_path / "conf")
    assert expanded.runtime.command[0] == f"{tmp_path / 'venv'}/bin/python"
    assert expanded.runtime.command[3] == f"--conf={tmp_path / 'conf'}/mod.yaml"
    assert expanded.runtime.cwd == f"{tmp_path / 'state'}/modules/demo"
    assert expanded.runtime.pythonpath == f"{tmp_path / 'state'}/src"


def test_expand_leaves_the_original_alone(tmp_path: Path) -> None:
    m = Manifest.model_validate(manifest(runtime={"command": ["{venv}/bin/python"]}))
    m.expand(venv=tmp_path, state=tmp_path, config=tmp_path)
    assert m.runtime.command == ["{venv}/bin/python"]


def test_expand_ignores_unknown_placeholders(tmp_path: Path) -> None:
    m = Manifest.model_validate(manifest(runtime={"command": ["{home}/bin/python"]}))
    expanded = m.expand(venv=tmp_path, state=tmp_path, config=tmp_path)
    assert expanded.runtime.command == ["{home}/bin/python"]


def test_expand_keeps_everything_else(tmp_path: Path) -> None:
    m = Manifest.model_validate(
        manifest(tools={"light_turn_on": {"class": "write"}}, audit={"redact": ["TOKEN"]})
    )
    expanded = m.expand(venv=tmp_path, state=tmp_path, config=tmp_path)
    assert expanded.tools["light_turn_on"].cls == "write"
    assert expanded.audit.redact == ["TOKEN"]


# --------------------------------------------------------------------------
# directories and diagnostics
# --------------------------------------------------------------------------
def test_load_manifests_reads_a_directory(tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        (tmp_path / f"{name}.yaml").write_text(Manifest.model_validate(manifest(name=name)).to_yaml())
    (tmp_path / "notes.txt").write_text("ignored")
    loaded = load_manifests(tmp_path)
    assert sorted(loaded) == ["alpha", "beta"]
    assert loaded["alpha"].runtime.command == ["/usr/bin/true"]


def test_load_manifests_on_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert load_manifests(tmp_path / "nope") == {}


def test_resolve_config_value_prefers_the_scoped_form() -> None:
    from vahub.contracts.manifest import module_env_prefix, resolve_config_value

    env = {"HA_TOKEN": "shared", "VAHUB_MOD_HOMEASSISTANT_HA_TOKEN": "scoped"}
    # The per-module value wins over a bare one of the same name.
    assert resolve_config_value("homeassistant", "HA_TOKEN", env) == "scoped"
    # A module with no scoped value still gets the bare one (backward compatible).
    assert resolve_config_value("evil", "HA_TOKEN", env) == "shared"
    assert resolve_config_value("m", "MISSING", {}) is None
    assert module_env_prefix("home-assistant") == "VAHUB_MOD_HOME_ASSISTANT_"


def test_load_manifests_skips_a_broken_manifest(tmp_path: Path) -> None:
    # As documented: one hand-edited, invalid file must not empty the whole
    # module set. The healthy manifest still loads; the broken one is skipped and
    # its error is available separately through manifest_errors().
    (tmp_path / "good.yaml").write_text(Manifest.model_validate(MINIMAL).to_yaml())
    (tmp_path / "bad.yaml").write_text("name: Bad Name\nruntime: {command: []}\n")
    loaded = load_manifests(tmp_path)
    assert list(loaded) == [MINIMAL["name"]]


def test_manifest_errors_reports_without_raising(tmp_path: Path) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(Manifest.model_validate(MINIMAL).to_yaml())
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: NOPE\nruntime: {command: [/bin/true]}\n")
    assert manifest_errors(good) == []
    assert len(manifest_errors(bad)) == 1
    assert "NOPE" in manifest_errors(bad)[0]


def test_published_json_schema_uses_the_wire_names() -> None:
    schema = as_json_schema()
    tool = schema["$defs"]["ToolSpec"]["properties"]
    assert "class" in tool and "cls" not in tool
