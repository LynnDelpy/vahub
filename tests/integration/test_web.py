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
            # These tests predate the built-in login and exercise the assistant
            # directly; auth is covered in test_auth. A test can turn it on by
            # passing auth={"enabled": True} via the runtime fixture's param.
            "web": {"origin_allowlist": [ALLOWED_ORIGIN], "auth": {"enabled": False}, **web},
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


async def test_benign_reads_do_not_require_an_origin(client) -> None:
    # A GET that changes nothing and leaks nothing stays lenient, so a plain
    # script is not broken by the Origin rule.
    assert (await client.get("/api/client-config", headers={"origin": HOSTILE_ORIGIN})).status_code == 200


async def test_pending_is_origin_checked(client) -> None:
    # /api/pending lists pending_ids, each of which confirms a destructive
    # action, so it is not a benign read: a cross-site page must not read it.
    assert (await client.get("/api/pending", headers={"origin": HOSTILE_ORIGIN})).status_code == 403
    assert (await client.get("/api/pending", headers={"origin": ALLOWED_ORIGIN})).status_code == 200


async def test_an_oversized_body_is_refused(client) -> None:
    # A chat message is capped at 8000 characters; a multi-megabyte POST is
    # refused by its declared length before it is buffered into memory.
    big = "x" * (128 * 1024)
    response = await client.post("/api/chat", json={"message": big}, headers={"origin": ALLOWED_ORIGIN})
    assert response.status_code == 413


# --------------------------------------------------------------------------
# origin rules
# --------------------------------------------------------------------------
async def test_a_cross_origin_post_is_refused(client) -> None:
    response = await client.post("/api/chat", json={"message": "hello"}, headers={"origin": HOSTILE_ORIGIN})
    assert response.status_code == 403


async def test_an_allowed_origin_gets_through(client) -> None:
    response = await client.post("/api/chat", json={"message": "hello"}, headers={"origin": ALLOWED_ORIGIN})
    assert response.status_code == 200


async def test_a_request_without_an_origin_is_a_non_browser_client(client) -> None:
    # curl and scripts send no Origin and have no session to ride.
    assert (await client.post("/api/chat", json={"message": "hello"})).status_code == 200


async def test_the_origin_rule_covers_confirmations(client) -> None:
    response = await client.post("/api/confirm/" + "a" * 32, headers={"origin": HOSTILE_ORIGIN})
    assert response.status_code == 403


@pytest.mark.parametrize("runtime", [{"origin_allowlist": ["*"]}], indirect=True)
async def test_a_wildcard_allowlist_disables_the_check(client) -> None:
    response = await client.post("/api/chat", json={"message": "hello"}, headers={"origin": HOSTILE_ORIGIN})
    assert response.status_code == 200


def test_a_websocket_from_a_hostile_origin_is_closed(app) -> None:
    # The handshake is a plain GET; no browser policy stops it being made.
    with TestClient(app) as test_client:
        connect = test_client.websocket_connect("/ws/events", headers={"origin": HOSTILE_ORIGIN})
        with pytest.raises(WebSocketDisconnect) as excinfo, connect as socket:
            socket.receive_json()
    assert excinfo.value.code == 1008


def test_a_websocket_from_an_allowed_origin_receives_the_snapshot(app) -> None:
    with (
        TestClient(app) as test_client,
        test_client.websocket_connect("/ws/events", headers={"origin": ALLOWED_ORIGIN}) as socket,
    ):
        message = socket.receive_json()
    # The socket exists to deliver approval prompts, not module telemetry.
    assert message["type"] == "ready"
    assert "modules" not in message


# --------------------------------------------------------------------------
# the operator surface must stay off the web
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/api/dev/call",  # executing an arbitrary tool without the agent or the gate
        "/api/modules/fake/logs",  # module stderr
        "/api/tools",  # the raw tool catalogue
        "/api/audit",  # the audit log
    ],
)
async def test_operator_routes_are_not_exposed(client, path: str) -> None:
    """The web is the assistant plus an owner's own management, not a debugger:
    module stderr, the raw tool catalogue, ungated tool invocation and the audit
    log stay on the CLI. (Module management, saved data and schedules are exposed,
    but only to a signed-in owner; see test_manage and test_modules. The owner's
    read-only tool endpoint is /api/tools/<module>/<tool>, not the catalogue.)"""
    for method in ("get", "post"):
        response = await getattr(client, method)(path, headers={"origin": ALLOWED_ORIGIN})
        assert response.status_code in (404, 405), f"{method.upper()} {path} answered {response.status_code}"


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
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


async def test_csp_nonce_is_bound_to_the_inline_tags(client) -> None:
    # A guard for the class of bug where the page shipped with a nonce CSP but no
    # nonce on its tags, so a real browser blocked its own script and style and
    # the assistant did not work at all. The served nonce must appear on the
    # inline <script> and <style>, and the placeholder must be gone.
    response = await client.get("/")
    html = response.text
    assert "__CSP_NONCE__" not in html
    match = re.search(r"script-src 'nonce-([^']+)'", response.headers["content-security-policy"])
    assert match, response.headers["content-security-policy"]
    nonce = match.group(1)
    assert f'<script nonce="{nonce}">' in html
    assert f'<style nonce="{nonce}">' in html


async def test_the_console_never_renders_module_text_as_html(client) -> None:
    # Module names, tool names, health details and stderr all come from code the
    # hub did not write. Assigning any of them into innerHTML is stored XSS in a
    # page that can unlock a door.
    body = (await client.get("/")).text
    assert not re.search(r"\b(inner|outer)HTML\s*=", body)
    assert "insertAdjacentHTML" not in body
    assert "document.write" not in body


async def test_responses_carry_the_hardening_headers(client) -> None:
    headers = (await client.get("/api/pending")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
