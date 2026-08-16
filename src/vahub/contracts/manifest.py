"""The module contract.

A module is a separate program that speaks MCP over stdin/stdout. It is not
imported by the hub and shares no memory with it: the hub spawns it, talks to it
over a pipe, and can kill it. That boundary is what lets a module be written by
someone else, in any language, and still be safe to run.

Each installed module has a manifest describing how to start it, what it needs,
and what it offers. `vahub module add` writes it; you do not normally hand-edit
one.

    name: homeassistant
    version: 0.2.0
    description: Lights, locks and sensors via Home Assistant

    runtime:
      command: ["{venv}/bin/python", "-m", "vahub_mod_homeassistant"]
      user: vahub-mod-homeassistant   # optional: drop to this uid (prod)
      cwd: "{state}/modules/homeassistant"

    config:
      required: [HA_URL, HA_TOKEN]
      optional: [HA_VERIFY_SSL]

    health:
      interval_s: 30
      timeout_s: 5

    restart:
      max_retries: 5
      backoff_base_s: 2
      reset_after_s: 600
      startup_timeout_s: 20

    audit:
      redact: [HA_TOKEN]

    tools:
      light_turn_on: { class: write }

Two rules that matter:

* The `tools` block is what the module *claims*. It is advisory. The gate in
  vahub.yaml decides what may actually be called; a module cannot grant itself
  permission by describing itself generously.
* Only the variables named in `config` are passed to the process; a module
  never sees the rest of the hub's environment. Each declared key is resolved
  per module first, as `VAHUB_MOD_<NAME>_<KEY>`, so a secret provided that way
  reaches only its own module even if another module's manifest names the same
  key. A bare `<KEY>` in the hub environment still works but is shared: any
  module that declares it receives it, and the supervisor logs when that
  happens. Provide secrets in the scoped form to keep one module's token out of
  another's reach.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

MANIFEST_SCHEMA_VERSION = 1

# A tool named __health is reserved: the hub calls it to distinguish "the
# process is alive" from "the thing it talks to is reachable". It never appears
# in the catalog offered to the model.
HEALTH_TOOL = "__health"

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
TOOL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeSpec(Strict):
    # Placeholders {venv}, {state} and {config} are expanded when the module is
    # spawned, so a manifest stays valid across machines with different layouts.
    command: list[str] = Field(min_length=1)
    user: str | None = None
    cwd: str | None = None
    # Extra import path. Only used for modules installed from a local checkout.
    pythonpath: str | None = None

    @field_validator("command")
    @classmethod
    def _no_shell(cls, v: list[str]) -> list[str]:
        # A command is an argv list, never a shell string: no interpolation, no
        # word splitting, nothing for a crafted value to escape into.
        if any(not isinstance(part, str) for part in v):
            raise ValueError("command must be a list of strings")
        return v


class ConfigSpec(Strict):
    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


class HealthSpec(Strict):
    interval_s: float = Field(30.0, gt=0)
    timeout_s: float = Field(5.0, gt=0)


class RestartSpec(Strict):
    max_retries: int = Field(5, ge=0)
    backoff_base_s: float = Field(2.0, gt=1)
    # A module that ran cleanly for this long has its failure count forgiven;
    # without it, five unrelated blips over a year add up to a dead module.
    reset_after_s: float = Field(600.0, gt=0)
    startup_timeout_s: float = Field(20.0, gt=0)


class AuditSpec(Strict):
    redact: list[str] = Field(default_factory=list)


class ToolSpec(Strict):
    cls: Literal["read", "write", "destructive"] = Field("read", alias="class")
    description: str | None = None
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Manifest(Strict):
    schema_version: int = MANIFEST_SCHEMA_VERSION
    name: str
    version: str = "0.0.0"
    description: str = ""
    homepage: str | None = None
    runtime: RuntimeSpec
    config: ConfigSpec = Field(default_factory=ConfigSpec)
    health: HealthSpec = Field(default_factory=HealthSpec)
    restart: RestartSpec = Field(default_factory=RestartSpec)
    audit: AuditSpec = Field(default_factory=AuditSpec)
    tools: dict[str, ToolSpec] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(
                f"invalid module name {v!r}: lowercase letters, digits, _ and - only"
            )
        return v

    @field_validator("tools")
    @classmethod
    def _valid_tools(cls, v: dict[str, ToolSpec]) -> dict[str, ToolSpec]:
        for tool in v:
            if tool.startswith("__"):
                raise ValueError(f"{tool!r} is reserved; names starting with __ are internal")
            if not TOOL_RE.match(tool):
                raise ValueError(f"invalid tool name {tool!r}")
        return v

    def expand(self, *, venv: Path, state: Path, config: Path) -> Manifest:
        """Resolve {venv}/{state}/{config} placeholders into concrete paths."""
        mapping = {"venv": str(venv), "state": str(state), "config": str(config)}

        def sub(text: str) -> str:
            for key, value in mapping.items():
                text = text.replace("{" + key + "}", value)
            return text

        data = self.model_dump(by_alias=True)
        data["runtime"]["command"] = [sub(part) for part in data["runtime"]["command"]]
        for key in ("cwd", "pythonpath"):
            if data["runtime"].get(key):
                data["runtime"][key] = sub(data["runtime"][key])
        return Manifest.model_validate(data)

    @staticmethod
    def from_file(path: Path) -> Manifest:
        data = yaml.safe_load(path.read_text()) or {}
        return Manifest.model_validate(data)

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(by_alias=True, exclude_none=True), sort_keys=False, width=100
        )


def load_manifests(directory: Path) -> dict[str, Manifest]:
    """Read every manifest in a directory. A broken one is skipped rather than
    preventing every other module from starting."""
    out: dict[str, Manifest] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.yaml")):
        manifest = Manifest.from_file(path)
        out[manifest.name] = manifest
    return out


def manifest_errors(path: Path) -> list[str]:
    """Validate without raising, for `vahub doctor`."""
    try:
        Manifest.from_file(path)
    except Exception as e:
        return [str(e)]
    return []


def as_json_schema() -> dict[str, Any]:
    """Published so module authors can validate their manifest in CI."""
    return Manifest.model_json_schema(by_alias=True)
