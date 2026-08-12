"""Shared fixtures.

Two decisions worth stating.

Every test runs with VAHUB_* stripped from the environment. Those variables are
the highest-precedence config source, and deployments do set them, so a test
asserting `web.port == 8080` would otherwise pass or fail depending on whose
shell it ran in. FAKE_* is stripped for the same reason: it steers the test
module in tests/integration/fake_module.py.

Hub objects are built through `build()`, which passes only the arguments the
constructor actually declares. The alternative is a fixture that has to be
rewritten every time a component gains an optional dependency, which makes the
test suite an obstacle to changing the code rather than a check on it.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).parent
FAKE_MODULE = TESTS_DIR / "integration" / "fake_module.py"


# --------------------------------------------------------------------------
# hermetic environment
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _strip_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(("VAHUB_", "FAKE_")):
            monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------
# construction helpers
# --------------------------------------------------------------------------
def build(factory: Callable[..., Any], **candidates: Any) -> Any:
    """Call `factory` with the subset of `candidates` its signature declares.

    Raises TypeError naming the gap when a required parameter has no candidate,
    which is a far more useful failure than a bare "unexpected keyword".
    """
    params = inspect.signature(factory).parameters
    accepted = {name: value for name, value in candidates.items() if name in params}
    missing = [
        name
        for name, p in params.items()
        if p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        and name not in accepted
        and name != "self"
    ]
    if missing:
        raise TypeError(
            f"{getattr(factory, '__qualname__', factory)} requires {missing}; "
            f"the test offered {sorted(candidates)}"
        )
    return factory(**accepted)


async def maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def until(predicate: Callable[[], Any], timeout: float = 8.0, interval: float = 0.02) -> Any:
    """Poll until `predicate` is truthy. Polling beats a fixed sleep: the test
    finishes as soon as the condition holds and still fails rather than hangs."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await maybe_await(predicate())
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"condition never held within {timeout}s (last value: {last!r})")


@pytest.fixture
def wait_for() -> Callable[..., Any]:
    return until


@pytest.fixture
def construct() -> Callable[..., Any]:
    return build


# --------------------------------------------------------------------------
# paths and configuration
# --------------------------------------------------------------------------
@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture
def modules_dir(tmp_path: Path) -> Path:
    path = tmp_path / "modules.d"
    path.mkdir()
    return path


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    def _write(text: str, name: str = "vahub.yaml") -> Path:
        path = tmp_path / name
        path.write_text(text)
        return path

    return _write


@pytest.fixture
def config(state_dir: Path, modules_dir: Path) -> Any:
    """A Config pointing at this test's temporary directories, otherwise default."""
    from vahub.config.models import Config

    return Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir), "log_format": "console"},
            "web": {"origin_allowlist": ["http://localhost:8080"]},
        }
    )


# --------------------------------------------------------------------------
# a manifest for the fake module
# --------------------------------------------------------------------------
FAKE_ENV_KEYS = [
    "FAKE_NAME",
    "FAKE_NO_TOOLS_CAP",
    "FAKE_HANDSHAKE_HANG",
    "FAKE_EXIT_AFTER",
    "FAKE_STDERR",
]


def fake_manifest_data(name: str = "fake", **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "description": "test module",
        "runtime": {"command": [sys.executable, "-u", str(FAKE_MODULE)]},
        # Only these keys reach the child. Anything else in the hub's own
        # environment must stay invisible to it.
        "config": {"required": [], "optional": list(FAKE_ENV_KEYS)},
        "health": {"interval_s": 0.2, "timeout_s": 2.0},
        "restart": {
            "max_retries": 2,
            "backoff_base_s": 1.05,
            "reset_after_s": 600,
            "startup_timeout_s": 10,
        },
        "audit": {"redact": ["secret"]},
        "tools": {"echo": {"class": "read"}, "crash": {"class": "destructive"}},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return data


@pytest.fixture
def write_manifest(modules_dir: Path) -> Callable[..., Any]:
    """Write a manifest for the fake module into the modules directory.

    It is validated through Manifest before being written, so a test cannot
    accidentally exercise the hub with a manifest the contract would reject.
    """
    from vahub.contracts.manifest import Manifest

    def _write(name: str = "fake", **overrides: Any) -> Manifest:
        manifest = Manifest.model_validate(fake_manifest_data(name, **overrides))
        (modules_dir / f"{name}.yaml").write_text(manifest.to_yaml())
        return manifest

    return _write


# --------------------------------------------------------------------------
# hub objects
# --------------------------------------------------------------------------
@pytest.fixture
def bus() -> Any:
    from vahub.core.bus import EventBus

    return EventBus()


@pytest.fixture
async def store(state_dir: Path) -> Any:
    from vahub.storage.store import Store

    store = build(Store, path=state_dir / "vahub.db", db_path=state_dir / "vahub.db")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def supervisor(bus: Any, config: Any, modules_dir: Path, state_dir: Path) -> Any:
    """A started Supervisor over whatever manifests the test wrote, torn down
    afterwards so no child process outlives the test."""
    from vahub.core.supervisor import Supervisor

    sup = build(
        Supervisor,
        bus=bus,
        config=config,
        hub=config.hub,
        modules_dir=modules_dir,
        state_dir=state_dir,
        # Where a manifest's {config} placeholder resolves to. The manifests
        # these tests write live alongside the module definitions.
        config_dir=modules_dir.parent,
    )
    try:
        yield sup
    finally:
        await maybe_await(sup.stop())


@pytest.fixture
def spawn(supervisor: Any) -> Callable[..., Any]:
    """discover() + start(), as one await, because every integration test needs
    both and neither is interesting on its own. Idempotent, so a test that waits
    for two modules does not start each of them twice."""
    started = False

    async def _spawn() -> Any:
        nonlocal started
        if not started:
            started = True
            await maybe_await(supervisor.discover())
            await maybe_await(supervisor.start())
        return supervisor

    return _spawn


@pytest.fixture
def ready(spawn: Callable[..., Any], supervisor: Any) -> Callable[..., Any]:
    """Start the supervisor and wait for one module to reach `ready`."""

    async def _ready(name: str = "fake", timeout: float = 15.0) -> Any:
        from vahub.core.supervisor import State

        await spawn()
        await until(lambda: supervisor.modules[name].state == State.READY, timeout=timeout)
        return supervisor.modules[name]

    return _ready


@pytest.fixture
def collect() -> Iterator[Callable[[Any, str], list]]:
    """Subscribe to a bus topic and collect what is published, in the background."""
    tasks: list[asyncio.Task] = []

    def _collect(bus: Any, topic: str) -> list:
        events: list = []
        sub = bus.subscribe(topic)

        async def pump() -> None:
            async for event in sub.events():
                events.append(event)

        tasks.append(asyncio.create_task(pump()))
        return events

    yield _collect
    for task in tasks:
        task.cancel()
