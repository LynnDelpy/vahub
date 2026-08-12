"""The module contract, checked with the kit that ships to module authors.

`vahub module verify` is what a third-party author runs in their own CI, so the
kit itself has to be trustworthy in both directions: it must pass a module that
honours the contract, and it must fail one that does not. Both are asserted
here, the second against modules broken on purpose, because a verifier that
passes everything is worse than none at all.

tests/integration/fake_module.py doubles as the reference implementation: if a
change to the contract makes it fail, either the module or the contract is
wrong, and this file is where that shows up.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from vahub.contracts.manifest import Manifest
from vahub.modules.verify import verify_manifest, verify_source

pytestmark = [pytest.mark.contract, pytest.mark.integration]

FAKE_MODULE = Path(__file__).resolve().parent.parent / "integration" / "fake_module.py"

# Everything the fake module offers, so `manifest_complete` has nothing to warn
# about. __health is deliberately absent: it is reserved and cannot be declared.
FAKE_TOOLS = {
    "echo": {"class": "read"},
    "add": {"class": "read"},
    "env_names": {"class": "read"},
    "stats": {"class": "read"},
    "set_health": {"class": "write"},
    "boom": {"class": "read"},
    "sleep": {"class": "read"},
    "big": {"class": "read"},
    "instructions": {"class": "read"},
    "nondict": {"class": "read"},
    "badid": {"class": "read"},
    "oversized": {"class": "read"},
    "srvreq": {"class": "read"},
    "crash": {"class": "destructive"},
    "secretive": {"class": "write"},
}


def manifest_for(command: list[str], **overrides) -> Manifest:
    data = {
        "name": "fake",
        "version": "1.0.0",
        "runtime": {"command": command},
        "restart": {"startup_timeout_s": 15},
        "tools": FAKE_TOOLS,
    }
    data.update(overrides)
    return Manifest.model_validate(data)


async def verify(manifest: Manifest, tmp_path: Path, **kwargs):
    return await verify_manifest(
        manifest,
        venv=tmp_path / "venv",
        state=tmp_path / "state",
        config_dir=tmp_path,
        health_timeout_s=10.0,
        **kwargs,
    )


@pytest.fixture
def report(tmp_path: Path):
    async def _report(manifest: Manifest, **kwargs):
        return await verify(manifest, tmp_path, **kwargs)

    return _report


def names_of(report, ok: bool | None = None) -> set[str]:
    return {c.name for c in report.checks if ok is None or c.ok is ok}


# --------------------------------------------------------------------------
# the reference module passes
# --------------------------------------------------------------------------
async def test_the_reference_module_satisfies_the_contract(report) -> None:
    result = await report(manifest_for([sys.executable, "-u", str(FAKE_MODULE)]))
    assert result.ok, result.text()
    assert result.failures == []


async def test_the_contract_covers_what_the_hub_relies_on(report) -> None:
    result = await report(manifest_for([sys.executable, "-u", str(FAKE_MODULE)]))
    # These are the checks the hub's own behaviour depends on. A kit that stopped
    # running one of them would still be green while the hub broke.
    assert {
        "handshake",
        "tools_list",
        "tool_names",
        "manifest_tools",
        "health_tool",
        "health_call",
        "health_shape",
        "shutdown",
    } <= names_of(result)


async def test_the_reference_module_reports_its_tools(report) -> None:
    result = await report(manifest_for([sys.executable, "-u", str(FAKE_MODULE)]))
    assert "echo" in result.tools
    assert all(not name.startswith("__") for name in result.tools), "reserved names stay hidden"


async def test_the_reference_module_leaves_stdout_to_the_protocol(report) -> None:
    result = await report(manifest_for([sys.executable, "-u", str(FAKE_MODULE)]))
    stdout_clean = next(c for c in result.checks if c.name == "stdout_clean")
    assert stdout_clean.ok, stdout_clean.detail


async def test_the_reference_module_exits_when_stdin_closes(report) -> None:
    # Closing stdin is how the hub asks a module to stop. A module that needs a
    # signal loses whatever it was doing every time the hub restarts.
    result = await report(manifest_for([sys.executable, "-u", str(FAKE_MODULE)]))
    shutdown = next(c for c in result.checks if c.name == "shutdown")
    assert shutdown.ok, shutdown.detail


async def test_a_missing_declaration_is_reported(report) -> None:
    # The manifest names a tool the module does not offer, which is how a policy
    # rule ends up silently never applying.
    manifest = manifest_for(
        [sys.executable, "-u", str(FAKE_MODULE)],
        tools={**FAKE_TOOLS, "not_offered": {"class": "read"}},
    )
    result = await report(manifest)
    assert not result.ok
    assert "manifest_tools" in names_of(result, ok=False)


async def test_an_undeclared_tool_is_a_warning_not_a_failure(report) -> None:
    # Offering more than the manifest lists is untidy, not unsafe: the gate
    # decides what may be called regardless.
    manifest = manifest_for([sys.executable, "-u", str(FAKE_MODULE)], tools={"echo": {"class": "read"}})
    result = await report(manifest)
    assert result.ok, result.text()
    assert "manifest_complete" in {c.name for c in result.warnings}


# --------------------------------------------------------------------------
# broken modules fail
# --------------------------------------------------------------------------
def write_module(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "broken_module.py"
    path.write_text(body)
    return [sys.executable, "-u", str(path)]


async def test_a_program_that_is_not_a_module_fails(report, tmp_path: Path) -> None:
    command = write_module(tmp_path, "import time\ntime.sleep(30)\n")
    result = await report(
        manifest_for(command, restart={"startup_timeout_s": 2}), startup_timeout_s=2
    )
    assert not result.ok
    assert "handshake" in names_of(result, ok=False)


async def test_a_module_without_the_health_tool_fails(report, tmp_path: Path) -> None:
    # Without the probe the hub can watch the process but not the thing it talks
    # to, which is the difference between `ready` and `degraded`.
    command = write_module(
        tmp_path,
        HEALTHLESS_MODULE,
    )
    result = await report(manifest_for(command, tools={"echo": {"class": "read"}}))
    assert not result.ok
    assert "health_tool" in names_of(result, ok=False)


async def test_a_health_payload_of_the_wrong_shape_fails(report, tmp_path: Path) -> None:
    command = write_module(tmp_path, BAD_HEALTH_MODULE)
    result = await report(manifest_for(command, tools={"echo": {"class": "read"}}))
    assert not result.ok
    assert "health_shape" in names_of(result, ok=False)


async def test_a_missing_executable_fails_without_raising(report, tmp_path: Path) -> None:
    result = await report(manifest_for(["/nonexistent/vahub-module"]))
    assert not result.ok
    assert "spawn" in names_of(result, ok=False)


async def test_an_invalid_manifest_is_a_report_not_an_exception(tmp_path: Path) -> None:
    source = tmp_path / "module.yaml"
    source.write_text("name: Not A Module Name\nruntime: {command: []}\n")
    result = await verify_source(source)
    assert not result.ok
    assert "manifest" in names_of(result, ok=False)


async def test_a_report_serialises_for_ci(report) -> None:
    result = await report(manifest_for([sys.executable, "-u", str(FAKE_MODULE)]))
    payload = result.as_dict()
    assert payload["ok"] is True
    assert payload["module"] == "fake"
    assert all({"name", "ok", "severity"} <= set(check) for check in payload["checks"])
    assert isinstance(result.text(), str)


# --------------------------------------------------------------------------
# module sources used only by this file
# --------------------------------------------------------------------------
HEALTHLESS_MODULE = '''
import json, sys

TOOLS = [{"name": "echo", "description": "echo", "inputSchema": {"type": "object", "properties": {}}}]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\\n")
    sys.stdout.flush()


while True:
    line = sys.stdin.readline()
    if not line:
        break
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "healthless", "version": "1"}}})
    elif msg.get("method") == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": TOOLS}})
    elif msg.get("id") is not None:
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": []}})
'''

BAD_HEALTH_MODULE = '''
import json, sys

TOOLS = [
    {"name": "echo", "description": "echo", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "__health", "description": "health", "inputSchema": {"type": "object", "properties": {}}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\\n")
    sys.stdout.flush()


while True:
    line = sys.stdin.readline()
    if not line:
        break
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "badhealth", "version": "1"}}})
    elif msg.get("method") == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": TOOLS}})
    elif msg.get("id") is not None:
        # A string where the contract asks for {"ok": bool, ...}.
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": "fine, thanks"}]}})
'''
