"""The tool catalog: what exists right now, under one namespace.

Names are namespaced `module.tool` because two modules may reasonably both
offer `status`, and the model needs one unambiguous string to call.

The catalog is built from what each ready module actually reported over MCP, not
from the `tools` block of its manifest. The manifest is a description written by
the module's author; the live list is what the process is really offering. The
manifest class is carried alongside as advisory metadata for the UI.

The agent-facing view is intersected with the policy: a tool the principal could
never call is not shown at all, so the model does not spend a turn planning a
call that would only die at the gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..contracts.manifest import HEALTH_TOOL
from .supervisor import State, Supervisor

if TYPE_CHECKING:
    from ..agent.policy import Gate


class Catalog:
    def __init__(self, supervisor: Supervisor, gate: Gate | None = None) -> None:
        self._sup = supervisor
        self._gate = gate

    def list_tools(self) -> list[dict[str, Any]]:
        """Every callable tool on every ready module."""
        out: list[dict[str, Any]] = []
        for mod in self._sup.modules.values():
            if mod.state is not State.READY:
                continue
            declared = mod.manifest.tools
            for tool in mod.tools:
                name = tool.get("name")
                if not isinstance(name, str) or name.startswith("__") or name == HEALTH_TOOL:
                    continue  # reserved surface stays out of the catalog
                spec = declared.get(name)
                description = tool.get("description")
                out.append(
                    {
                        "name": f"{mod.name}.{name}",
                        "module": mod.name,
                        "tool": name,
                        "description": description if isinstance(description, str) else None,
                        "input_schema": tool.get("inputSchema"),
                        # Advisory only: the gate decides, a manifest cannot
                        # grant its own module permission.
                        "declared_class": spec.cls if spec is not None else None,
                    }
                )
        return sorted(out, key=lambda t: t["name"])

    def for_principal(self, principal: str = "agent") -> list[dict[str, Any]]:
        tools = self.list_tools()
        if self._gate is None:
            return tools
        return [t for t in tools if self._gate.visible_to(principal, t["module"], t["tool"])]

    # The agent loop knows this view by name.
    agent_catalog = for_principal

    def resolve(self, name: str) -> tuple[str, str] | None:
        """Split a namespaced `module.tool` name. Returns None when the name is
        not one of ours, which a caller must treat as "unknown tool" rather than
        guessing at a module."""
        module, _, tool = name.partition(".")
        if not module or not tool:
            return None
        if any(t["module"] == module and t["tool"] == tool for t in self.list_tools()):
            return module, tool
        return None

    def unavailable_modules(self) -> list[str]:
        """Modules the agent should be told about, so it says "the lights are
        unreachable" instead of inventing a reason for a missing answer."""
        return sorted(
            mod.name for mod in self._sup.modules.values() if mod.state in (State.DEGRADED, State.FAILED)
        )
