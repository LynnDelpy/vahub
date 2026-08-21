"""Module management and the owner read-tool endpoint over REST.

Auth is off in this fixture so the endpoint logic is exercised directly; the
login guard that protects these paths when auth is on is covered in test_auth.
The fake module is a real child process, so the read-tool endpoint is tested
end to end: the owner's browser call reaches a module and gets its answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from vahub.config.models import Config
from vahub.core.supervisor import State
from vahub.web.app import create_app

pytestmark = pytest.mark.integration

ALLOWED_ORIGIN = "http://localhost:8080"


def _config(state_dir: Path, modules_dir: Path) -> Config:
    return Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "web": {"origin_allowlist": [ALLOWED_ORIGIN], "auth": {"enabled": False}},
            "llm": {"provider": "mock"},
            # crash is declared destructive; the owner read path must refuse it.
            "policy": {
                "default": "deny",
                "principals": {"agent": {"confirm": ["destructive"]}},
                "rules": {"fake.crash": {"class": "destructive"}},
            },
        }
    )


@pytest.fixture
async def rt(construct, state_dir: Path, modules_dir: Path, write_manifest, wait_for):
    from vahub.core.runtime import Runtime

    write_manifest("fake")
    runtime = construct(
        Runtime, config=_config(state_dir, modules_dir), config_path=modules_dir.parent / "x.yaml"
    )
    await runtime.store.open()
    runtime.supervisor.set_db_config(await runtime.store.all_module_config())
    runtime.supervisor.discover()
    await runtime.supervisor.start()
    await wait_for(lambda: runtime.supervisor.modules["fake"].state == State.READY, timeout=15.0)
    try:
        yield runtime
    finally:
        await runtime.supervisor.stop()
        await runtime.store.close()


@pytest.fixture
async def client(rt):
    transport = httpx.ASGITransport(app=create_app(rt))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as c:
        c.headers["origin"] = ALLOWED_ORIGIN
        yield c


# --------------------------------------------------------------------------
# listing
# --------------------------------------------------------------------------
async def test_list_modules_reports_state_tools_and_config(client) -> None:
    mods = (await client.get("/api/modules")).json()["modules"]
    fake = next(m for m in mods if m["name"] == "fake")
    assert fake["state"] == "ready"
    echo = next(t for t in fake["tools"] if t["name"] == "echo")
    assert echo["class"] == "read"
    # The UI builds real form fields from the live schema, so it is passed through.
    assert echo["schema"] is None or isinstance(echo["schema"], dict)
    assert "FAKE_NAME" in fake["config"]["optional"]
    assert fake["config"]["set"] == []  # nothing configured through the UI yet


# --------------------------------------------------------------------------
# the owner read-tool endpoint (what a dashboard card calls)
# --------------------------------------------------------------------------
async def test_owner_can_call_a_read_tool(client) -> None:
    r = await client.post("/api/tools/fake/echo", json={"args": {"text": "hi"}})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["result"]["args"] == {"text": "hi"}


async def test_owner_cannot_call_a_destructive_tool(client) -> None:
    # crash would kill the module if it ran; the read-only guard must refuse it
    # before dispatch, so the module stays up.
    r = await client.post("/api/tools/fake/crash", json={"args": {}})
    assert r.status_code == 400 and r.json()["error"] == "not_readonly"


async def test_owner_cannot_call_a_tool_not_declared_read(client) -> None:
    # `add` exists at runtime but the manifest does not declare it read, so the
    # owner path will not run it: only manifest-declared read tools are callable.
    r = await client.post("/api/tools/fake/add", json={"args": {"a": 1, "b": 2}})
    assert r.status_code == 400 and r.json()["error"] == "not_readonly"


async def test_owner_call_of_the_health_probe_is_refused(client) -> None:
    r = await client.post("/api/tools/fake/__health", json={"args": {}})
    assert r.status_code == 400 and r.json()["error"] == "reserved_tool"


async def test_tool_call_is_origin_checked(client) -> None:
    r = await client.post(
        "/api/tools/fake/echo",
        json={"args": {"text": "x"}},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------
# per-module configuration (tokens entered in the UI)
# --------------------------------------------------------------------------
async def test_setting_a_declared_key_stores_it_without_leaking_the_value(client, rt) -> None:
    r = await client.put("/api/modules/fake/config/FAKE_NAME", json={"value": "hello"})
    assert r.status_code == 200 and r.json()["ok"] is True

    cfg = (await client.get("/api/modules/fake/config")).json()
    assert "FAKE_NAME" in cfg["set"]  # the key is shown as set...
    assert "hello" not in json.dumps(cfg)  # ...but never the value

    assert await rt.store.module_config("fake") == {"FAKE_NAME": "hello"}


async def test_deleting_a_config_key(client, rt) -> None:
    await client.put("/api/modules/fake/config/FAKE_NAME", json={"value": "hello"})
    assert (await client.delete("/api/modules/fake/config/FAKE_NAME")).json()["ok"] is True
    assert await rt.store.module_config("fake") == {}


async def test_setting_an_undeclared_key_is_refused(client) -> None:
    r = await client.put("/api/modules/fake/config/NOT_DECLARED", json={"value": "x"})
    assert r.status_code == 400 and r.json()["error"] == "undeclared_key"


async def test_config_of_an_unknown_module_is_404(client) -> None:
    assert (await client.get("/api/modules/ghost/config")).status_code == 404


async def test_config_writes_are_origin_checked(client) -> None:
    r = await client.put(
        "/api/modules/fake/config/FAKE_NAME",
        json={"value": "x"},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------
# the registry-backed "available" list
# --------------------------------------------------------------------------
async def test_available_reads_a_local_registry(client, tmp_path, monkeypatch) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": "2026-08-16",
                "modules": {
                    "weather": {
                        "description": "weather",
                        "latest": "0.1.0",
                        "versions": {"0.1.0": {"source": {"type": "path", "path": "/x"}}},
                    }
                },
            }
        )
    )
    monkeypatch.setenv("VAHUB_REGISTRY_URL", str(registry))
    body = (await client.get("/api/modules/available")).json()
    names = [m["name"] for m in body["available"]]
    assert "weather" in names


async def test_available_degrades_to_empty_without_a_registry(client, monkeypatch) -> None:
    # An unreachable index must not 500 the modules page.
    monkeypatch.setenv("VAHUB_REGISTRY_URL", "https://127.0.0.1:1/nope.json")
    body = (await client.get("/api/modules/available")).json()
    assert body["available"] == []
