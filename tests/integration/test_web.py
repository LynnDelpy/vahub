"""The HTTP and WebSocket surface, driven in-process over ASGI.

The hub authenticates nobody, so the check that carries the most weight here is
the Origin rule: without it, a page on any other site could make the operator's
browser drive the hub, and the WebSocket has never been covered by the
same-origin policy at all. The rest of this file is about the surface staying
dull under bad input: unknown ids, oversized bodies, a disabled endpoint.

The runtime is real (a Runtime with a mock model and no modules) rather than a
mock, because what is being tested is the wiring.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vahub.config.models import Config
from vahub.web.app import create_app

pytestmark = pytest.mark.integration

ALLOWED_ORIGIN = "http://localhost:8080"
HOSTILE_ORIGIN = "https://evil.example"


def make_config(state_dir: Path, modules_dir: Path, **web: Any) -> Config:
    return Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "web": {"origin_allowlist": [ALLOWED_ORIGIN], **web},
            "llm": {"provider": "mock"},
            "policy": {"default": "deny", "rules": {}},
        }
    )


@pytest.fixture
async def runtime(construct, state_dir: Path, modules_dir: Path, request):
    """A Runtime with its store open and no modules discovered."""
    from vahub.core.runtime import Runtime

    overrides = getattr(request, "param", {}) or {}
    config = make_config(state_dir, modules_dir, **overrides)
    rt = construct(Runtime, config=config, config_path=modules_dir.parent / "vahub.yaml")
    await rt.store.open()
    rt.supervisor.discover()
    try:
        yield rt
    finally:
        await rt.supervisor.stop()
        await rt.store.close()


@pytest.fixture
def app(runtime):
    return create_app(runtime)


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------
async def test_health_answers_while_the_process_is_up(client) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_reports_settled_modules(client) -> None:
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json()["ready"] is True


async def test_metrics_are_exposed_in_prometheus_format(client) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "# HELP" in response.text


async def test_reads_do_not_require_an_origin(client) -> None:
    # GET routes change nothing, so refusing them on Origin would only break
    # legitimate scripts.
    assert (await client.get("/api/modules", headers={"origin": HOSTILE_ORIGIN})).status_code == 200


# --------------------------------------------------------------------------
# origin rules
# --------------------------------------------------------------------------
async def test_a_cross_origin_post_is_refused(client) -> None:
    response = await client.post(
        "/api/chat", json={"message": "hello"}, headers={"origin": HOSTILE_ORIGIN}
    )
    assert response.status_code == 403


async def test_an_allowed_origin_gets_through(client) -> None:
    response = await client.post(
        "/api/chat", json={"message": "hello"}, headers={"origin": ALLOWED_ORIGIN}
    )
    assert response.status_code == 200


async def test_a_request_without_an_origin_is_a_non_browser_client(client) -> None:
    # curl and scripts send no Origin and have no session to ride.
    assert (await client.post("/api/chat", json={"message": "hello"})).status_code == 200


async def test_the_origin_rule_covers_confirmations(client) -> None:
    response = await client.post(
        "/api/confirm/" + "a" * 32, headers={"origin": HOSTILE_ORIGIN}
    )
    assert response.status_code == 403


@pytest.mark.parametrize("runtime", [{"origin_allowlist": ["*"]}], indirect=True)
async def test_a_wildcard_allowlist_disables_the_check(client) -> None:
    response = await client.post(
        "/api/chat", json={"message": "hello"}, headers={"origin": HOSTILE_ORIGIN}
    )
    assert response.status_code == 200


def test_a_websocket_from_a_hostile_origin_is_closed(app) -> None:
    # The handshake is a plain GET; no browser policy stops it being made.
    with TestClient(app) as test_client:
        connect = test_client.websocket_connect("/ws/events", headers={"origin": HOSTILE_ORIGIN})
        with pytest.raises(WebSocketDisconnect) as excinfo, connect as socket:
            socket.receive_json()
    assert excinfo.value.code == 1008


def test_a_websocket_from_an_allowed_origin_receives_the_snapshot(app) -> None:
    with TestClient(app) as test_client, test_client.websocket_connect(
        "/ws/events", headers={"origin": ALLOWED_ORIGIN}
    ) as socket:
        message = socket.receive_json()
    assert message["type"] == "snapshot"
    assert message["modules"] == []


# --------------------------------------------------------------------------
# the dev endpoint
# --------------------------------------------------------------------------
async def test_the_dev_endpoint_is_off_by_default(client) -> None:
    response = await client.post(
        "/api/dev/call",
        json={"module": "fake", "tool": "echo", "args": {}},
        headers={"origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("runtime", [{"dev_tools_endpoint": True}], indirect=True)
async def test_the_dev_endpoint_still_goes_through_the_call_path(client) -> None:
    response = await client.post(
        "/api/dev/call",
        json={"module": "fake", "tool": "echo", "args": {}},
        headers={"origin": ALLOWED_ORIGIN},
    )
    # No module is installed, so the answer is a structured error rather than a
    # crash, and it arrived through ModuleAPI like every other call.
    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "unknown_module", "detail": "fake"}


@pytest.mark.parametrize("runtime", [{"dev_tools_endpoint": True}], indirect=True)
async def test_the_dev_endpoint_rejects_a_malformed_module_name(client) -> None:
    response = await client.post(
        "/api/dev/call",
        json={"module": "../../etc", "tool": "echo"},
        headers={"origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("runtime", [{"dev_tools_endpoint": True}], indirect=True)
async def test_oversized_arguments_are_refused(client) -> None:
    response = await client.post(
        "/api/dev/call",
        json={"module": "fake", "tool": "echo", "args": {"blob": "x" * 20_000}},
        headers={"origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 413


# --------------------------------------------------------------------------
# chat
# --------------------------------------------------------------------------
async def test_chat_answers_with_the_mock_model(client) -> None:
    response = await client.post(
        "/api/chat", json={"message": "hello"}, headers={"origin": ALLOWED_ORIGIN}
    )
    body = response.json()
    assert response.status_code == 200
    assert body["session_id"]
    assert isinstance(body["reply"], str) and body["reply"]
    assert body["steps"] == []  # no modules are installed, so nothing was called


async def test_chat_keeps_a_session(client) -> None:
    first = (
        await client.post("/api/chat", json={"message": "hello"}, headers={"origin": ALLOWED_ORIGIN})
    ).json()
    second = (
        await client.post(
            "/api/chat",
            json={"message": "again", "session_id": first["session_id"]},
            headers={"origin": ALLOWED_ORIGIN},
        )
    ).json()
    assert second["session_id"] == first["session_id"]


async def test_an_empty_message_is_refused(client) -> None:
    response = await client.post(
        "/api/chat", json={"message": ""}, headers={"origin": ALLOWED_ORIGIN}
    )
    assert response.status_code == 422


async def test_an_oversized_message_is_refused(client) -> None:
    response = await client.post(
        "/api/chat", json={"message": "x" * 20_000}, headers={"origin": ALLOWED_ORIGIN}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
async def test_module_listing_is_empty_without_modules(client) -> None:
    assert (await client.get("/api/modules")).json() == []


async def test_logs_for_an_unknown_module_are_a_404(client) -> None:
    assert (await client.get("/api/modules/nope/logs")).status_code == 404


async def test_the_tool_catalog_is_json(client) -> None:
    assert (await client.get("/api/tools")).json() == []


async def test_the_audit_log_is_readable(client) -> None:
    response = await client.get("/api/audit")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


async def test_confirming_an_unknown_id_is_not_an_error_page(client) -> None:
    response = await client.post("/api/confirm/" + "b" * 32, headers={"origin": ALLOWED_ORIGIN})
    assert response.status_code == 200
    assert response.json()["error"] == "unknown_pending"


# --------------------------------------------------------------------------
# the console page
# --------------------------------------------------------------------------
async def test_the_console_page_is_served(client) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


async def test_the_console_never_renders_module_text_as_html(client) -> None:
    # Module names, tool names, health details and stderr all come from code the
    # hub did not write. Assigning any of them into innerHTML is stored XSS in a
    # page that can unlock a door.
    body = (await client.get("/")).text
    assert not re.search(r"\b(inner|outer)HTML\s*=", body)
    assert "insertAdjacentHTML" not in body
    assert "document.write" not in body


async def test_responses_carry_the_hardening_headers(client) -> None:
    headers = (await client.get("/api/modules")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
