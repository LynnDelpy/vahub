"""Loading and validating `vahub.yaml`.

Precedence, lowest to highest:
    built-in defaults  <  vahub.yaml  <  VAHUB_* environment variables

The environment override exists for container deployments, where injecting one
variable is easier than templating a file. `VAHUB_WEB__PORT=9000` sets
`web.port`; `__` separates levels. Only scalar leaves can be overridden this
way, which is deliberate: nobody should be expressing a policy rule as an
environment variable.

Validation errors are reported with the path of the offending key and the file
it came from, because a config error at startup is something a human has to fix
under time pressure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import Config, ConfigError, interpolate

ENV_PREFIX = "VAHUB_"

# Search order when no explicit path is given.
DEFAULT_PATHS = (
    Path("vahub.yaml"),
    Path("/etc/vahub/vahub.yaml"),
)


def default_config_path() -> Path:
    """Where the config would be read from, for messages and for `vahub init`."""
    if env := os.environ.get("VAHUB_CONFIG"):
        return Path(env)
    for candidate in DEFAULT_PATHS:
        if candidate.is_file():
            return candidate
    return DEFAULT_PATHS[-1]


def _coerce(raw: str) -> Any:
    """Env vars are strings; YAML/JSON scalars are not. Parse where it is
    unambiguous so VAHUB_WEB__PORT=9000 becomes an int and a JSON list works."""
    text = raw.strip()
    if text[:1] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            return raw
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none", "~"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return raw


def _env_overrides() -> dict[str, Any]:
    """Build a partial config tree from VAHUB_* variables.

    Only variables whose first segment names an actual top-level section are
    treated as overrides. That distinction matters: secrets are *referenced*
    from the config as ${VAHUB_LLM_API_KEY}, so those variables are present in
    the environment too, and interpreting them as config keys would turn every
    secret into an "unknown field" error at startup.
    """
    sections = set(Config.model_fields)
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX) or key == "VAHUB_CONFIG":
            continue
        path = key[len(ENV_PREFIX) :].lower().split("__")
        if not path or not path[0] or path[0] not in sections:
            continue
        cursor = out
        for part in path[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[path[-1]] = _coerce(value)
    return out


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _format_validation_error(err: ValidationError, source: str) -> str:
    lines = [f"{source} is not valid:"]
    for detail in err.errors():
        location = ".".join(str(p) for p in detail["loc"]) or "(root)"
        lines.append(f"  {location}: {detail['msg']}")
    return "\n".join(lines)


def load_config(path: str | Path | None = None, *, strict_secrets: bool = True) -> Config:
    """Read, interpolate, merge and validate. Raises ConfigError with a message
    intended to be printed straight to a terminal."""
    resolved = Path(path) if path is not None else default_config_path()

    raw: dict[str, Any] = {}
    if resolved.is_file():
        try:
            loaded = yaml.safe_load(resolved.read_text())
        except yaml.YAMLError as e:
            raise ConfigError(f"{resolved} is not valid YAML:\n  {e}") from e
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{resolved} must contain a mapping at the top level")
        raw = loaded

    raw = interpolate(raw, strict=strict_secrets)
    merged = _deep_merge(raw, _env_overrides())

    try:
        return Config.model_validate(merged)
    except ValidationError as e:
        source = str(resolved) if resolved.is_file() else "the configuration"
        raise ConfigError(_format_validation_error(e, source)) from e


def config_exists(path: str | Path | None = None) -> bool:
    return (Path(path) if path else default_config_path()).is_file()
