"""Config loading: strictness, interpolation, and precedence.

The bias in these tests is that a config mistake must be loud. A typo that
silently produces an empty allowlist is worse than a crash at startup, so most
of what is asserted here is that something raises and that the message names the
offending key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vahub.config.loader import default_config_path, load_config
from vahub.config.models import Config, ConfigError, interpolate


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "absent.yaml")
    assert isinstance(cfg, Config)
    assert cfg.web.port == 8080
    assert cfg.web.host == "127.0.0.1"  # loopback by default, exposure is deliberate
    assert cfg.policy.default == "deny"


def test_empty_file_is_not_an_error(write_config) -> None:
    assert load_config(write_config("")).llm.provider == "mock"


def test_values_from_file_win_over_defaults(write_config) -> None:
    cfg = load_config(write_config("web:\n  port: 9999\n  host: 0.0.0.0\n"))
    assert (cfg.web.host, cfg.web.port) == ("0.0.0.0", 9999)


# --------------------------------------------------------------------------
# strictness
# --------------------------------------------------------------------------
def test_unknown_top_level_key_is_an_error(write_config) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config("wob:\n  port: 9000\n"))
    assert "wob" in str(excinfo.value)


def test_unknown_nested_key_is_reported_with_its_path(write_config) -> None:
    path = write_config("web:\n  origin_allowlst: []\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert "web.origin_allowlst" in message
    assert str(path) in message  # the human has to know which file to open


def test_unknown_key_deep_inside_the_policy_is_an_error(write_config) -> None:
    text = """
policy:
  rules:
    home.light_turn_on:
      class: write
      constraints:
        entity_id:
          regex: "^light\\\\."
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(text))
    assert "regex" in str(excinfo.value)


def test_top_level_must_be_a_mapping(write_config) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write_config("- one\n- two\n"))


def test_invalid_yaml_names_the_file(write_config) -> None:
    path = write_config("web: [unclosed\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert str(path) in str(excinfo.value)


def test_out_of_range_port_is_rejected(write_config) -> None:
    with pytest.raises(ConfigError, match="port"):
        load_config(write_config("web:\n  port: 70000\n"))


def test_unknown_literal_value_is_rejected(write_config) -> None:
    with pytest.raises(ConfigError, match="log_level"):
        load_config(write_config("hub:\n  log_level: VERBOSE\n"))


# --------------------------------------------------------------------------
# interpolation
# --------------------------------------------------------------------------
def test_env_reference_is_expanded(write_config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_LLM_KEY", "sk-from-env")
    cfg = load_config(write_config("llm:\n  api_key: ${MY_LLM_KEY}\n"))
    assert cfg.llm.api_key == "sk-from-env"


def test_missing_env_reference_explains_itself(write_config) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config("llm:\n  api_key: ${DEFINITELY_NOT_SET}\n"))
    message = str(excinfo.value)
    assert "DEFINITELY_NOT_SET" in message
    assert "not set" in message


def test_env_reference_default_is_used_when_unset(write_config) -> None:
    cfg = load_config(write_config("llm:\n  model: ${NO_SUCH_VAR:-fallback-model}\n"))
    assert cfg.llm.model == "fallback-model"


def test_file_reference_is_read_and_stripped(write_config, tmp_path: Path) -> None:
    secret = tmp_path / "llm_key"
    secret.write_text("sk-from-file\n")
    cfg = load_config(write_config(f"llm:\n  api_key: ${{file:{secret}}}\n"))
    assert cfg.llm.api_key == "sk-from-file"


def test_missing_secret_file_is_an_error(write_config, tmp_path: Path) -> None:
    missing = tmp_path / "not-there"
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(f"llm:\n  api_key: ${{file:{missing}}}\n"))
    assert str(missing) in str(excinfo.value)


def test_secrets_can_be_skipped_for_inspection(write_config, tmp_path: Path) -> None:
    # `vahub config show` on a machine without the secrets must still work.
    cfg = load_config(write_config(f"llm:\n  api_key: ${{file:{tmp_path / 'nope'}}}\n"), strict_secrets=False)
    assert cfg.llm.api_key == ""


def test_interpolation_reaches_nested_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED", "https://home.example")
    data = {"web": {"origin_allowlist": ["${ALLOWED}", "http://localhost:8080"]}}
    assert interpolate(data)["web"]["origin_allowlist"][0] == "https://home.example"


def test_interpolation_leaves_non_strings_alone() -> None:
    data = {"web": {"port": 8080, "origin_allowlist": None}}
    assert interpolate(data) == data


# --------------------------------------------------------------------------
# environment overrides
# --------------------------------------------------------------------------
def test_env_override_beats_the_file(write_config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAHUB_WEB__PORT", "9000")
    cfg = load_config(write_config("web:\n  port: 8080\n  host: 0.0.0.0\n"))
    assert cfg.web.port == 9000
    assert cfg.web.host == "0.0.0.0"  # untouched keys survive the merge


def test_env_override_coerces_scalars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VAHUB_HUB__LOG_FORMAT", "console")
    monkeypatch.setenv("VAHUB_BUDGETS__WALL_CLOCK_TEXT_S", "2.5")
    monkeypatch.setenv("VAHUB_WEB__ORIGIN_ALLOWLIST", '["https://a.example"]')
    monkeypatch.setenv("VAHUB_BUDGETS__TOKENS_PER_DAY", "null")
    cfg = load_config(tmp_path / "absent.yaml")
    assert cfg.hub.log_format == "console"
    assert cfg.budgets.wall_clock_text_s == 2.5
    assert cfg.web.origin_allowlist == ["https://a.example"]
    assert cfg.budgets.tokens_per_day is None


def test_string_env_override_is_not_mis_coerced(monkeypatch, tmp_path: Path) -> None:
    # A string setting that looks like a keyword or a number must stay a string.
    # VAHUB_SPEECH__STT__PROVIDER=none targets a Literal[..., "none"] field; the
    # loader used to coerce "none" to Python None and fail validation. A model id
    # that is all digits must likewise stay a string, not become an int.
    monkeypatch.setenv("VAHUB_SPEECH__STT__PROVIDER", "none")
    monkeypatch.setenv("VAHUB_LLM__MODEL", "9000")
    cfg = load_config(tmp_path / "absent.yaml")
    assert cfg.speech.stt.provider == "none"
    assert cfg.llm.model == "9000"


def test_env_override_of_an_unknown_key_is_still_an_error(monkeypatch, tmp_path: Path) -> None:
    # Strictness must not be bypassable by using the environment instead of YAML.
    monkeypatch.setenv("VAHUB_WEB__PROT", "9000")
    with pytest.raises(ConfigError, match="prot"):
        load_config(tmp_path / "absent.yaml")


def test_vahub_config_is_not_treated_as_an_override(monkeypatch, tmp_path: Path, write_config) -> None:
    path = write_config("web:\n  port: 8123\n")
    monkeypatch.setenv("VAHUB_CONFIG", str(path))
    assert load_config().web.port == 8123
    assert default_config_path() == path


# --------------------------------------------------------------------------
# policy validation at load time
# --------------------------------------------------------------------------
def test_bad_regex_in_a_constraint_fails_at_load(write_config) -> None:
    # A broken regex must not wait until someone asks the assistant to open a
    # lock to make itself known.
    text = """
policy:
  rules:
    home.light_turn_on:
      class: write
      constraints:
        entity_id:
          matches: "^light\\\\.(unclosed"
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(text))
    message = str(excinfo.value)
    assert "matches" in message
    assert "regex" in message


def test_a_valid_policy_loads_with_its_constraints(write_config) -> None:
    text = """
policy:
  default: deny
  confirm_ttl_s: 30
  principals:
    agent:
      confirm: [destructive]
      deny: ["*.lock_*"]
  rules:
    home.light_turn_on:
      class: write
      constraints:
        entity_id:
          matches: "^light\\\\."
        brightness_pct:
          range: [1, 100]
        mode:
          in: [auto, manual]
"""
    policy = load_config(write_config(text)).policy
    rule = policy.rules["home.light_turn_on"]
    assert rule.cls == "write"
    assert rule.constraints["brightness_pct"].range == (1.0, 100.0)
    assert rule.constraints["mode"].in_ == ["auto", "manual"]
    assert policy.principals["agent"].deny == ["*.lock_*"]


def test_allow_by_default_with_no_rules_is_refused(write_config) -> None:
    with pytest.raises(ConfigError, match="deny"):
        load_config(write_config("policy:\n  default: allow\n"))


def test_duplicate_schedule_ids_are_refused(write_config) -> None:
    text = """
schedules:
  - id: morning
    cron: "0 7 * * *"
  - id: morning
    cron: "0 8 * * *"
"""
    with pytest.raises(ConfigError, match="morning"):
        load_config(write_config(text))


def test_schedule_steps_are_parsed(write_config) -> None:
    text = """
schedules:
  - id: morning
    cron: "0 7 * * *"
    steps:
      - module: home
        tool: light_turn_on
        args: {entity_id: light.kitchen}
        timeout_s: 5
"""
    schedule = load_config(write_config(text)).schedules[0]
    assert schedule.enabled is True
    assert schedule.steps[0].args == {"entity_id": "light.kitchen"}


def test_db_path_derives_from_the_state_dir(write_config, tmp_path: Path) -> None:
    cfg = load_config(write_config(f"hub:\n  state_dir: {tmp_path}\n"))
    assert cfg.hub.db_path == tmp_path / "vahub.db"
