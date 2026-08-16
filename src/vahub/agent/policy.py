"""The policy gate. Default deny, consulted on every tool call.

This is a security boundary in code, not in the prompt. It sits in front of the
module API, so the agent, the scheduler and a confirmed destructive action all
pass through the same check regardless of what the model believes it is allowed to do.

Two decisions are worth knowing about:

* The gate checks arguments, not just tool names. A module usually holds one
  broad credential (a Home Assistant admin token, for instance), so
  `light_turn_on` is only meaningful as a permission if the entities it may
  touch are constrained. An argument the rule does not describe is rejected
  rather than waved through.
* A principal entry can only subtract (deny globs) or escalate (confirm
  classes). Permission comes from `policy.rules` alone, so adding or misspelling
  a principal can never widen what is reachable.

Denials carry a reason written for the person who has to fix the config: it names
the rule path they need to edit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Literal

from ..config.models import Config, Constraint, PolicyConfig, Principal, Rule, ToolClass

Outcome = Literal["allow", "deny", "confirm"]

# Used when policy.default is "allow" and no rule names the tool. It carries no
# constraints, which is why an argument-bearing call is still denied there: an
# unconstrained argument is the thing this gate exists to prevent.
_OPEN_RULE = Rule()


@dataclass(frozen=True, slots=True)
class Decision:
    """The gate's verdict. `reason` is empty only when the outcome is allow."""

    outcome: Outcome
    reason: str = ""
    cls: ToolClass = "read"

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


class Gate:
    def __init__(self, policy: PolicyConfig | None = None) -> None:
        self._policy = policy or PolicyConfig()
        self._default_deny = self._policy.default == "deny"
        self._rules: dict[str, Rule] = dict(self._policy.rules)
        self._principals: dict[str, Principal] = dict(self._policy.principals)
        # Patterns are validated at config load; compiling them once here keeps
        # the hot path free of regex parsing.
        self._patterns: dict[tuple[str, str], re.Pattern[str]] = {}
        for key, rule in self._rules.items():
            for arg, constraint in rule.constraints.items():
                if constraint.matches is not None:
                    self._patterns[(key, arg)] = re.compile(constraint.matches)

    @classmethod
    def from_config(cls, config: Config) -> Gate:
        return cls(config.policy)

    @property
    def confirm_ttl_s(self) -> float:
        return self._policy.confirm_ttl_s

    def cls_for(self, module: str, tool: str) -> ToolClass:
        """The class the policy assigns to a tool, for display and for audit."""
        rule = self._rules.get(f"{module}.{tool}")
        return rule.cls if rule is not None else "read"

    def visible_to(self, principal: str, module: str, tool: str) -> bool:
        """Catalog filter: could this tool ever be allowed for this principal,
        ignoring arguments? Tools that fail here are hidden from the model so it
        does not plan calls that would only die at the gate."""
        key = f"{module}.{tool}"
        if self._denied_by_principal(principal, key):
            return False
        return key in self._rules or not self._default_deny

    def evaluate(self, principal: str, module: str, tool: str, args: Any = None) -> Decision:
        key = f"{module}.{tool}"
        rule = self._rules.get(key)
        cls: ToolClass = rule.cls if rule is not None else "read"

        if self._denied_by_principal(principal, key):
            return Decision(
                "deny",
                f"{key} is denied for principal {principal!r} by policy.principals.{principal}.deny",
                cls,
            )

        if rule is None:
            if self._default_deny:
                return Decision(
                    "deny",
                    f"no policy rule for {key} and policy.default is deny; "
                    f"add a rule under policy.rules.{key!r} to permit it",
                    cls,
                )
            rule = _OPEN_RULE

        if args is None:
            args = {}
        if not isinstance(args, dict):
            return Decision("deny", f"{key}: arguments must be an object", cls)

        for name, value in args.items():
            constraint = rule.constraints.get(name)
            if constraint is None:
                return Decision("deny", self._unconstrained_reason(key, name), cls)
            failure = self._check(key, name, constraint, value)
            if failure is not None:
                return Decision("deny", f"{key}: argument {name!r} {failure}", cls)

        principal_spec = self._principals.get(principal)
        if principal_spec is not None and cls in principal_spec.confirm:
            return Decision(
                "confirm",
                f"{key} is a {cls} action and principal {principal!r} must confirm it",
                cls,
            )
        return Decision("allow", "", cls)

    # --- internals --------------------------------------------------------
    def _denied_by_principal(self, principal: str, key: str) -> bool:
        spec = self._principals.get(principal)
        if spec is None:
            return False
        return any(fnmatch(key, pattern) for pattern in spec.deny)

    def _unconstrained_reason(self, key: str, name: str) -> str:
        if key in self._rules:
            return (
                f"{key}: argument {name!r} is not permitted; "
                f"list it under policy.rules.{key!r}.constraints to allow it"
            )
        # Reachable only with policy.default=allow, where no rule means no
        # constraints, and an unconstrained argument is exactly what we refuse.
        return (
            f"{key}: argument {name!r} is not permitted because policy.rules has no entry for "
            f"{key!r}; add one with a constraints entry for {name!r}"
        )

    def _check(self, key: str, name: str, constraint: Constraint, value: Any) -> str | None:
        """Return a human-readable failure, or None when the value is allowed.

        A constraint entry with no fields set means "any value": that is how a
        rule deliberately opens one argument up."""
        if constraint.in_ is not None and value not in constraint.in_:
            return f"value {value!r} is not one of {constraint.in_!r}"

        if constraint.matches is not None:
            if not isinstance(value, str):
                return f"value {value!r} must be a string matching {constraint.matches!r}"
            pattern = self._patterns.get((key, name)) or re.compile(constraint.matches)
            if pattern.search(value) is None:
                return f"value {value!r} does not match {constraint.matches!r}"

        if constraint.range is not None:
            low, high = constraint.range
            # bool is an int in Python, and True passing a 0..100 range check is
            # never what the rule author meant.
            if isinstance(value, bool) or not isinstance(value, int | float):
                return f"value {value!r} must be a number between {low} and {high}"
            if not low <= value <= high:
                return f"value {value!r} is outside the allowed range [{low}, {high}]"

        if constraint.max_len is not None:
            size = len(value) if isinstance(value, str | list | dict) else len(str(value))
            if size > constraint.max_len:
                return f"is {size} long, the limit is {constraint.max_len}"

        return None
