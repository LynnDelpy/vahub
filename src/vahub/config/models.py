"""The configuration contract.

One file configures the whole hub: `vahub.yaml`. It holds the runtime settings,
the language model, the budgets, the policy that authorizes every action, and
the scheduled routines. Nested structures (policy rules with argument
constraints, multi-step routines) are why this is YAML and not a flat env file.

Secrets are never written in it. Values may reference the environment or a file:

    api_key: ${VAHUB_LLM_API_KEY}          # from the environment
    api_key: ${file:/run/secrets/llm_key}  # from a file (systemd credentials,
                                           # docker secrets, k8s secrets)

Every setting can also be overridden by an environment variable using the
VAHUB_ prefix and __ for nesting (VAHUB_WEB__PORT=9000), which is what container
deployments usually want.

Loading is strict: unknown keys are an error, not a silently ignored typo. A
misspelled `origin_allowlist` must fail loudly rather than ship an empty,
fail-open list.
"""

from __future__ import annotations

import os
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    """Every config section rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# runtime
# --------------------------------------------------------------------------
class HubConfig(Strict):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    timezone: str = "UTC"
    # Where the database, module venvs and other runtime state live.
    state_dir: Path = Path("/var/lib/vahub")
    # Where installed module manifests are written by `vahub module add`.
    modules_dir: Path = Path("/etc/vahub/modules.d")

    @property
    def db_path(self) -> Path:
        return self.state_dir / "vahub.db"


# Keys that used to exist under `web`. `extra="forbid"` would reject them with a
# generic "extra inputs are not permitted", which tells someone upgrading nothing
# about what replaced them.
REMOVED_WEB_KEYS: dict[str, str] = {
    "dev_tools_endpoint": (
        "the web surface no longer executes tools directly; use the CLI on the "
        "host (vahub module verify, vahub audit) instead"
    ),
}


class AuthConfig(Strict):
    """The hub's own login. When enabled, the web routes require a session from a
    successful sign in; accounts are created with `vahub user add`, never by the
    hub itself. When disabled, the hub trusts a reverse proxy as before."""

    # On by default: a hub reachable by a browser should not be open. With it on
    # and no accounts yet, the page says to create one rather than letting anyone
    # in. Turn it off only when a proxy in front already authenticates.
    enabled: bool = True
    # How long a login lasts before it must be repeated.
    session_ttl_s: float = Field(60 * 60 * 24 * 14, gt=0)
    # The Secure flag on the session cookie. Leave on for a real (HTTPS)
    # deployment; a plain-http LAN test has to turn it off or the browser drops
    # the cookie and every login appears to fail.
    cookie_secure: bool = True


class WebConfig(Strict):
    @model_validator(mode="before")
    @classmethod
    def _explain_removed_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, why in REMOVED_WEB_KEYS.items():
                if key in data:
                    raise ValueError(f"web.{key} was removed: {why}")
        return data

    # Loopback by default: exposing the hub to a network is a deliberate act.
    host: str = "127.0.0.1"
    port: int = Field(8080, ge=1, le=65535)
    # Browser origins allowed to call the API. Same-origin does not stop a
    # cross-origin POST, and it does not apply to WebSockets at all, so this is
    # checked explicitly on both. "*" disables the check.
    origin_allowlist: list[str] = Field(default_factory=lambda: ["http://localhost:8080"])
    # Header the authenticating reverse proxy sets. Recorded in the audit log as
    # the acting principal. Informational: it is never an authorization input.
    auth_subject_header: str = "X-Auth-Subject"
    # The hub's built-in login.
    auth: AuthConfig = Field(default_factory=AuthConfig)


class BudgetConfig(Strict):
    """Bounds on a single conversation turn. An iteration cap alone does not
    bound cost: one unfiltered tool result can dwarf a whole conversation."""

    iterations_per_turn: int = Field(8, ge=1, le=100)
    tool_result_bytes: int = Field(16384, ge=256)
    tokens_per_turn: int = Field(20_000, ge=0)
    tokens_per_day: int | None = None
    wall_clock_text_s: float = Field(30.0, gt=0)
    wall_clock_voice_s: float = Field(8.0, gt=0)


class LLMConfig(Strict):
    provider: Literal["openai_compat", "anthropic", "mock"] = "mock"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str | None = None
    model: str = "mock"
    temperature: float = Field(0.2, ge=0, le=2)
    max_tokens: int = Field(1024, ge=1)
    request_timeout_s: float = Field(60.0, gt=0)
    system_prompt: str | None = None


class STTConfig(Strict):
    # "browser" means the client transcribes locally (no credentials, no audio
    # leaves the machine). "openai_compat" posts audio to a Whisper-style API.
    provider: Literal["browser", "openai_compat", "none"] = "browser"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    model: str = "whisper-1"
    request_timeout_s: float = Field(60.0, gt=0)


class TTSConfig(Strict):
    provider: Literal["browser", "openai_compat", "none"] = "browser"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    model: str = "tts-1"
    voice: str = "alloy"
    request_timeout_s: float = Field(60.0, gt=0)


class SpeechConfig(Strict):
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------
class Constraint(Strict):
    """Allowed values for one tool argument. An argument with no constraint
    entry is rejected: the gate never waves through what it was not told about."""

    in_: list[Any] | None = Field(None, alias="in")
    # A regex the whole value must match (re.fullmatch, not search): the pattern
    # describes the entire argument, so "light\\.[a-z_]+" allows "light.kitchen"
    # but not "light.kitchen extra" or a leading/trailing anything.
    matches: str | None = None
    range: tuple[float, float] | None = None
    max_len: int | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("matches")
    @classmethod
    def _valid_regex(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:  # a broken regex must fail at load, not at call time
                raise ValueError(f"invalid regex {v!r}: {e}") from e
        return v


ToolClass = Literal["read", "write", "destructive"]


class Rule(Strict):
    cls: ToolClass = Field("read", alias="class")
    constraints: dict[str, Constraint] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Principal(Strict):
    """Who is acting. The agent, the scheduler and a human at the console are
    deliberately not the same: the scheduler may act unattended but must not
    touch locks, the agent must ask before anything destructive."""

    confirm: list[ToolClass] = Field(default_factory=list)
    # fnmatch globs over "module.tool". fnmatch has no substring shortcut, so a
    # verb must be wrapped in stars to match bare names: "*unlock*" matches both
    # "ha.unlock" and "door.unlock_front", where "*.unlock_*" matches neither of
    # the first kind.
    deny: list[str] = Field(default_factory=list)  # e.g. ["*unlock*", "*delete*"]


class PolicyConfig(Strict):
    default: Literal["deny", "allow"] = "deny"
    # How long a pending destructive confirmation stays valid.
    confirm_ttl_s: float = Field(60.0, gt=0)
    principals: dict[str, Principal] = Field(default_factory=dict)
    rules: dict[str, Rule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _warn_on_allow(self) -> PolicyConfig:
        if self.default == "allow" and not self.rules:
            raise ValueError(
                "policy.default=allow with no rules gives the model unrestricted "
                "control; set default=deny and list what is permitted"
            )
        return self

    @model_validator(mode="after")
    def _destructive_must_be_confirmed(self) -> PolicyConfig:
        """A `destructive` class is only meaningful if some principal confirms
        it. The agent principal runs untrusted model output, so a destructive
        tool it can reach without confirmation is a fail-open gate: the label
        does nothing and a prompt injection unlocks the door with no human. This
        refuses to load such a policy rather than run it silently.

        The agent principal is the one the loop drives (it is named "agent"). A
        principal that deliberately runs destructive actions directly, like a
        human at the console, is not constrained here; only the agent is.
        """
        destructive = [key for key, rule in self.rules.items() if rule.cls == "destructive"]
        if not destructive:
            return self
        agent = self.principals.get("agent")
        if agent is None:
            raise ValueError(
                "policy has destructive rules "
                f"({', '.join(sorted(destructive))}) but no 'agent' principal. The agent runs "
                "untrusted model output and must be told to confirm destructive actions: add an "
                "'agent' principal with `confirm: [destructive]`."
            )
        if "destructive" in agent.confirm:
            return self
        reachable = [key for key in destructive if not any(fnmatch(key, pat) for pat in agent.deny)]
        if reachable:
            raise ValueError(
                "the 'agent' principal can invoke destructive tools without confirmation "
                f"({', '.join(sorted(reachable))}); add `destructive` to the agent's `confirm` "
                "list, or deny those tools for the agent. A destructive tool that needs no human "
                "is a `write`, not a `destructive`."
            )
        return self


# --------------------------------------------------------------------------
# schedules
# --------------------------------------------------------------------------
class ScheduleStep(Strict):
    module: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(10.0, gt=0)


class Schedule(Strict):
    id: str
    cron: str
    enabled: bool = True
    steps: list[ScheduleStep] = Field(default_factory=list)


# --------------------------------------------------------------------------
# root
# --------------------------------------------------------------------------
class Config(Strict):
    hub: HubConfig = Field(default_factory=HubConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    schedules: list[Schedule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _schedule_ids_unique(self) -> Config:
        seen = set()
        for s in self.schedules:
            if s.id in seen:
                raise ValueError(f"duplicate schedule id: {s.id!r}")
            seen.add(s.id)
        return self


# --------------------------------------------------------------------------
# interpolation
# --------------------------------------------------------------------------
_REF = re.compile(r"\$\{([^}]+)\}")


class ConfigError(Exception):
    """Raised with a message meant for a human fixing their config file."""


def _resolve_scalar(value: str, *, strict: bool) -> str:
    """Expand ${VAR} and ${file:/path} references inside a string."""

    def repl(m: re.Match[str]) -> str:
        ref = m.group(1).strip()
        if ref.startswith("file:"):
            path = Path(ref[5:].strip())
            try:
                return path.read_text().strip()
            except OSError as e:
                if strict:
                    raise ConfigError(f"cannot read secret file {path}: {e}") from e
                return ""
        default = None
        if ":-" in ref:
            ref, default = ref.split(":-", 1)
            ref = ref.strip()
        env = os.environ.get(ref)
        if env is not None:
            return env
        if default is not None:
            return default
        if strict:
            raise ConfigError(f"config references ${{{ref}}} but that environment variable is not set")
        return ""

    return _REF.sub(repl, value)


def interpolate(data: Any, *, strict: bool = True) -> Any:
    """Walk a loaded YAML tree expanding ${...} references in every string."""
    if isinstance(data, str):
        return _resolve_scalar(data, strict=strict)
    if isinstance(data, dict):
        return {k: interpolate(v, strict=strict) for k, v in data.items()}
    if isinstance(data, list):
        return [interpolate(v, strict=strict) for v in data]
    return data
