"""The user-facing management routes: saved data and schedules over REST.

Auth is off in this fixture so the endpoint logic is exercised directly; the
login guard that protects these paths when auth is on is covered in test_auth.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from vahub.config.models import Config
from vahub.web.app import create_app

pytestmark = pytest.mark.integration

ALLOWED_ORIGIN = "http://localhost:8080"


def _config(state_dir: Path, modules_dir: Path) -> Config:
    return Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "web": {"origin_allowlist": [ALLOWED_ORIGIN], "auth": {"enabled": False}},
            "llm": {"provider": "mock"},
            "policy": {"default": "deny", "rules": {}},
        }
    )


@pytest.fixture
async def rt(construct, state_dir: Path, modules_dir: Path):
    from vahub.core.runtime import Runtime

    runtime = construct(
        Runtime, config=_config(state_dir, modules_dir), config_path=modules_dir.parent / "x.yaml"
    )
    await runtime.store.open()
    runtime.supervisor.discover()
    try:
        yield runtime
    finally:
        await runtime.store.close()


@pytest.fixture
async def client(rt):
    transport = httpx.ASGITransport(app=create_app(rt))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as c:
        c.headers["origin"] = ALLOWED_ORIGIN
        yield c


async def test_locations_round_trip(client) -> None:
    r = await client.put("/api/locations/home", json={"label": "Home", "latitude": 47.4, "longitude": 9.4})
    assert r.status_code == 200 and r.json()["ok"] is True
    listing = (await client.get("/api/locations")).json()["locations"]
    assert [loc["name"] for loc in listing] == ["home"]
    assert (await client.delete("/api/locations/home")).json()["ok"] is True
    assert (await client.get("/api/locations")).json()["locations"] == []


async def test_location_validates_coordinates(client) -> None:
    r = await client.put("/api/locations/home", json={"latitude": 999})
    assert r.status_code == 422  # out of the -90..90 range


async def test_settings_and_memory_are_separated(client, rt) -> None:
    await client.put("/api/settings/units", json={"value": "metric"})
    # The preferences editor cannot write the assistant's memory namespace.
    rejected = await client.put("/api/settings/memory:anniversary", json={"value": "2018-06-01"})
    assert rejected.status_code == 400
    # Memory is set by the assistant (its gated core.remember tool), which the
    # store records under the memory: prefix.
    await rt.store.set_setting("memory:anniversary", "2018-06-01")

    body = (await client.get("/api/settings")).json()
    assert body["settings"] == {"units": "metric"}
    assert body["memory"] == {"anniversary": "2018-06-01"}


async def test_schedule_create_list_delete(client) -> None:
    created = await client.post(
        "/api/schedules",
        json={
            "cron": "0 7 * * *",
            "steps": [{"module": "time", "tool": "speak_current_time", "args": {}}],
            "description": "morning",
        },
    )
    assert created.status_code == 200
    sid = created.json()["id"]

    listing = (await client.get("/api/schedules")).json()["schedules"]
    assert any(s["id"] == sid and s["editable"] for s in listing)

    assert (await client.post(f"/api/schedules/{sid}/enabled", json={"enabled": False})).json()["ok"]
    assert (await client.delete(f"/api/schedules/{sid}")).json()["ok"] is True


async def test_bad_cron_is_rejected(client) -> None:
    r = await client.post(
        "/api/schedules",
        json={"cron": "not a cron", "steps": [{"module": "time", "tool": "x"}]},
    )
    assert r.status_code == 400 and r.json()["error"] == "bad_cron"


async def test_writes_are_origin_checked(client) -> None:
    r = await client.put(
        "/api/locations/home", json={"label": "x"}, headers={"origin": "https://evil.example"}
    )
    assert r.status_code == 403
