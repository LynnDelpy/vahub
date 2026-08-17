"""The policy gate.

This is the security boundary, so the tests are written from the attacker's
side: what does it take to get a call through that the operator did not intend?
The answers the gate must give are that an unlisted tool is denied, that an
argument nobody described is denied rather than passed along, and that the
class of an action decides whether a human is asked first.
"""

from __future__ import annotations

from typing import Any

import pytest

from vahub.agent.policy import Gate
from vahub.config.models import Config, PolicyConfig

POLICY: dict[str, Any] = {
    "default": "deny",
    "principals": {
        "agent": {"confirm": ["destructive"], "deny": []},
        "scheduler": {"confirm": [], "deny": ["*.lock_*", "home.light_turn_off"]},
        "user": {"confirm": [], "deny": []},
    },
    "rules": {
        "home.get_state": {"class": "read", "constraints": {"entity_id": {"matches": "sensor\\..+"}}},
        "home.light_turn_on": {
            "class": "write",
            "constraints": {
                "entity_id": {"in": ["light.kitchen", "light.hall"]},
                "brightness_pct": {"range": [1, 100]},
            },
        },
        "home.lock_unlock": {"class": "destructive", "constraints": {"entity_id": {"matches": "lock\\..+"}}},
        "home.light_turn_off": {"class": "write", "constraints": {"entity_id": {"matches": "light\\..+"}}},
        "notes.append": {"class": "write", "constraints": {"text": {"max_len": 10}}},
        "clock.now": {"class": "read"},
    },
}


def outcome(decision: Any) -> str:
    """The decision may carry a str or an enum; compare on the wire value."""
    return str(getattr(decision.outcome, "value", decision.outcome))


@pytest.fixture
def make_gate(construct):
    def _make(policy: dict[str, Any] | None = None) -> Gate:
        parsed = PolicyConfig.model_validate(policy if policy is not None else POLICY)
        return construct(Gate, policy=parsed, config=Config(policy=parsed))

    return _make


@pytest.fixture
def gate(make_gate) -> Gate:
    return make_gate()


# --------------------------------------------------------------------------
# default deny
# --------------------------------------------------------------------------
def test_a_tool_with_no_rule_is_denied(gate: Gate) -> None:
    decision = gate.evaluate("agent", "home", "reboot_host", {})
    assert outcome(decision) == "deny"
    assert "home.reboot_host" in decision.reason


def test_a_tool_from_an_unknown_module_is_denied(gate: Gate) -> None:
    # Names are matched whole: a rule for home.get_state must not cover
    # anotherhome.get_state.
    assert outcome(gate.evaluate("agent", "anotherhome", "get_state", {})) == "deny"


def test_a_listed_tool_with_no_arguments_is_allowed(gate: Gate) -> None:
    assert outcome(gate.evaluate("agent", "clock", "now", {})) == "allow"


def test_the_decision_carries_the_class_for_the_audit_log(gate: Gate) -> None:
    decision = gate.evaluate("agent", "home", "light_turn_on", {"entity_id": "light.kitchen"})
    assert outcome(decision) == "allow"
    assert decision.cls == "write"


# --------------------------------------------------------------------------
# argument constraints
# --------------------------------------------------------------------------
def test_an_argument_with_no_constraint_entry_is_denied(gate: Gate) -> None:
    # The rule describes entity_id and brightness_pct. `transition` is not
    # dangerous in itself; the point is that the gate never forwards an
    # argument the operator never considered.
    decision = gate.evaluate(
        "agent", "home", "light_turn_on", {"entity_id": "light.kitchen", "transition": 3600}
    )
    assert outcome(decision) == "deny"
    assert "transition" in decision.reason


def test_in_constraint(gate: Gate) -> None:
    assert outcome(gate.evaluate("agent", "home", "light_turn_on", {"entity_id": "light.kitchen"})) == "allow"
    denied = gate.evaluate("agent", "home", "light_turn_on", {"entity_id": "light.bedroom"})
    assert outcome(denied) == "deny"


def test_matches_constraint(gate: Gate) -> None:
    assert outcome(gate.evaluate("agent", "home", "get_state", {"entity_id": "sensor.temp"})) == "allow"
    assert outcome(gate.evaluate("agent", "home", "get_state", {"entity_id": "lock.front"})) == "deny"


def test_matches_is_a_full_match_not_a_search(make_gate) -> None:
    # `matches` is a whitelist: it must describe the WHOLE value. With re.search
    # each of these would be allowed because the pattern is found somewhere
    # inside; with re.fullmatch a leading or trailing extra, or a newline, is
    # rejected.
    gate = make_gate(
        {
            "default": "deny",
            "principals": {"agent": {"confirm": [], "deny": []}},
            "rules": {
                "home.get_state": {
                    "class": "read",
                    "constraints": {"entity_id": {"matches": "sensor\\.[a-z]+"}},
                }
            },
        }
    )

    def allow(v: str) -> str:
        return outcome(gate.evaluate("agent", "home", "get_state", {"entity_id": v}))

    assert allow("sensor.temp") == "allow"
    assert allow("sensor.temp; rm -rf") == "deny"  # trailing junk
    assert allow("xsensor.temp") == "deny"  # leading junk
    assert allow("sensor.temp\nlock.front") == "deny"  # newline injection


def test_matches_constraint_rejects_a_non_string(gate: Gate) -> None:
    # A model can emit any JSON. A regex check against an int must deny, not raise.
    assert outcome(gate.evaluate("agent", "home", "get_state", {"entity_id": 42})) == "deny"
    assert outcome(gate.evaluate("agent", "home", "get_state", {"entity_id": None})) == "deny"


def test_range_constraint(gate: Gate) -> None:
    ok = {"entity_id": "light.kitchen", "brightness_pct": 50}
    assert outcome(gate.evaluate("agent", "home", "light_turn_on", ok)) == "allow"
    for bad in (0, 101, -5):
        args = {"entity_id": "light.kitchen", "brightness_pct": bad}
        assert outcome(gate.evaluate("agent", "home", "light_turn_on", args)) == "deny"


def test_range_constraint_rejects_an_incomparable_value(gate: Gate) -> None:
    args = {"entity_id": "light.kitchen", "brightness_pct": "bright"}
    assert outcome(gate.evaluate("agent", "home", "light_turn_on", args)) == "deny"


def test_max_len_constraint(gate: Gate) -> None:
    assert outcome(gate.evaluate("agent", "notes", "append", {"text": "short"})) == "allow"
    assert outcome(gate.evaluate("agent", "notes", "append", {"text": "x" * 11})) == "deny"


def test_the_reason_names_the_argument_that_failed(gate: Gate) -> None:
    decision = gate.evaluate("agent", "home", "get_state", {"entity_id": "lock.front"})
    assert "entity_id" in decision.reason


# --------------------------------------------------------------------------
# principals
# --------------------------------------------------------------------------
def test_principal_deny_glob_beats_an_allowing_rule(gate: Gate) -> None:
    args = {"entity_id": "lock.front"}
    # The rule permits it and the argument is fine, but this principal may not.
    assert outcome(gate.evaluate("agent", "home", "lock_unlock", args)) == "confirm"
    denied = gate.evaluate("scheduler", "home", "lock_unlock", args)
    assert outcome(denied) == "deny"
    assert "scheduler" in denied.reason


def test_principal_deny_matches_an_exact_name_too(gate: Gate) -> None:
    args = {"entity_id": "light.kitchen"}
    assert outcome(gate.evaluate("agent", "home", "light_turn_off", args)) == "allow"
    assert outcome(gate.evaluate("scheduler", "home", "light_turn_off", args)) == "deny"


def test_star_wrapped_deny_glob_matches_bare_verb_tools(make_gate) -> None:
    # fnmatch has no substring match, so "*.unlock_*" catches "ha.unlock_front"
    # but never a tool named plainly "unlock". The scaffolded and documented form
    # is "*unlock*", which must catch both, or the scheduler's "no locks" rule is
    # silently inert.
    gate = make_gate(
        {
            "default": "deny",
            "principals": {"scheduler": {"confirm": [], "deny": ["*unlock*", "*delete*"]}},
            "rules": {
                "ha.unlock": {"class": "write", "constraints": {"door": {"max_len": 20}}},
                "notes.delete": {"class": "write", "constraints": {"id": {"max_len": 20}}},
            },
        }
    )
    assert outcome(gate.evaluate("scheduler", "ha", "unlock", {"door": "front"})) == "deny"
    assert outcome(gate.evaluate("scheduler", "notes", "delete", {"id": "1"})) == "deny"


def test_an_unknown_principal_gets_no_privileges_but_no_exemptions(gate: Gate) -> None:
    # No principal entry means no confirm list and no deny list: the rules alone
    # decide, and the default deny still applies.
    assert outcome(gate.evaluate("nobody", "clock", "now", {})) == "allow"
    assert outcome(gate.evaluate("nobody", "home", "reboot_host", {})) == "deny"


# --------------------------------------------------------------------------
# confirmation classes
# --------------------------------------------------------------------------
def test_destructive_requires_confirmation_for_the_agent(gate: Gate) -> None:
    decision = gate.evaluate("agent", "home", "lock_unlock", {"entity_id": "lock.front"})
    assert outcome(decision) == "confirm"
    assert decision.cls == "destructive"


def test_confirmation_is_per_principal(gate: Gate) -> None:
    # The person at the console confirmed by being there; the agent did not.
    assert outcome(gate.evaluate("user", "home", "lock_unlock", {"entity_id": "lock.front"})) == "allow"


def test_destructive_rule_without_agent_confirmation_is_refused_at_load() -> None:
    # Fail closed: a `destructive` rule the agent can reach without confirming is
    # a gate that does nothing, so the config must not load rather than run open.
    from pydantic import ValidationError

    base = {
        "default": "deny",
        "rules": {"home.lock_unlock": {"class": "destructive"}},
    }
    # No principals at all: the agent falls straight through to allow.
    with pytest.raises(ValidationError, match="destructive"):
        PolicyConfig.model_validate(base)
    # An agent principal that does not confirm destructive.
    with pytest.raises(ValidationError, match="without confirmation"):
        PolicyConfig.model_validate({**base, "principals": {"agent": {"confirm": ["write"], "deny": []}}})
    # Confirming, or denying the tool for the agent, both load.
    PolicyConfig.model_validate({**base, "principals": {"agent": {"confirm": ["destructive"], "deny": []}}})
    PolicyConfig.model_validate({**base, "principals": {"agent": {"confirm": [], "deny": ["*unlock*"]}}})


def test_a_bad_argument_is_denied_before_confirmation_is_offered(gate: Gate) -> None:
    # Otherwise a confirmation prompt becomes a way to launder a rejected value.
    decision = gate.evaluate("agent", "home", "lock_unlock", {"entity_id": "light.kitchen"})
    assert outcome(decision) == "deny"


def test_confirming_write_as_well_is_possible(make_gate) -> None:
    policy = {**POLICY, "principals": {"agent": {"confirm": ["write", "destructive"]}}}
    strict = make_gate(policy)
    args = {"entity_id": "light.kitchen"}
    assert outcome(strict.evaluate("agent", "home", "light_turn_on", args)) == "confirm"
    assert outcome(strict.evaluate("agent", "clock", "now", {})) == "allow"


# --------------------------------------------------------------------------
# catalog visibility
# --------------------------------------------------------------------------
def test_catalog_hides_what_could_never_be_allowed(gate: Gate) -> None:
    assert gate.visible_to("agent", "clock", "now") is True
    assert gate.visible_to("agent", "home", "reboot_host") is False


def test_catalog_hides_tools_denied_for_this_principal(gate: Gate) -> None:
    assert gate.visible_to("agent", "home", "lock_unlock") is True  # confirmable, so still offered
    assert gate.visible_to("scheduler", "home", "lock_unlock") is False


def test_catalog_visibility_ignores_arguments(gate: Gate) -> None:
    # Visibility is about whether the tool is ever callable. A tool whose
    # arguments happen to be wrong this time must stay in the catalog.
    assert gate.visible_to("agent", "home", "get_state") is True


# --------------------------------------------------------------------------
# default allow
# --------------------------------------------------------------------------
def test_default_allow_still_refuses_undescribed_arguments(make_gate) -> None:
    # `default: allow` is a foot-gun the config only permits alongside rules.
    # Even then an argument nobody constrained does not get through.
    permissive = make_gate({"default": "allow", "rules": {"clock.now": {"class": "read"}}})
    assert outcome(permissive.evaluate("agent", "anything", "at_all", {})) == "allow"
    assert outcome(permissive.evaluate("agent", "anything", "at_all", {"path": "/etc/shadow"})) == "deny"
    assert permissive.visible_to("agent", "anything", "at_all") is True
