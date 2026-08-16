"""The Runtime: one process, one startup order, one shutdown order.

Everything the hub owns is constructed here and torn down in reverse. The order
is not incidental. The store opens before anything can audit into it; modules
start before the scheduler, which may fire a routine the moment it starts; the
web server comes last, so nothing is reachable before it can be answered
correctly. On the way down the web server drains first, then the scheduler stops
firing, then modules get SIGTERM and, if they ignore it, SIGKILL, and only then
does the database close.

Collaborators (agent, storage, scheduler, speech, web) are imported inside the
constructor rather than at module import time: the web layer imports Runtime for
its type hints, and a cycle at import time would be a startup crash rather than
a design remark.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn

from ..config.loader import default_config_path
from ..config.models import Config
from . import metrics
from .bus import EventBus
from .catalog import Catalog
from .logging import configure as configure_logging
from .logging import get_logger
from .moduleapi import ModuleAPI
from .supervisor import Supervisor

if TYPE_CHECKING:
    from ..agent.policy import Gate
    from ..storage.store import Store

log = get_logger("runtime")


class Runtime:
    def __init__(self, config: Config, config_path: Path | None = None) -> None:
        from ..agent.llm import build_adapter
        from ..agent.loop import AgentLoop
        from ..agent.policy import Gate
        from ..agent.session import SessionStore
        from ..scheduler import Scheduler
        from ..speech import build_stt, build_tts
        from ..storage import Store

        self.config = config
        self.config_path = Path(config_path) if config_path else default_config_path()
        # {config} in a manifest points at the directory the config file lives
        # in, so a manifest can name a file next to it without an absolute path.
        self.config_dir = self.config_path.parent

        self.bus = EventBus()
        self.gate: Gate = Gate(config.policy)
        self.store: Store = Store(config.hub.db_path)
        self.supervisor = Supervisor(
            self.bus,
            modules_dir=config.hub.modules_dir,
            state_dir=config.hub.state_dir,
            config_dir=self.config_dir,
        )
        self.moduleapi = ModuleAPI(
            self.supervisor,
            gate=self.gate,
            store=self.store,
            bus=self.bus,
            confirm_ttl_s=config.policy.confirm_ttl_s,
        )
        self.catalog = Catalog(self.supervisor, gate=self.gate)
        # The web layer and the agent loop know the catalog under this name.
        self.registry = self.catalog

        self.llm = build_adapter(config.llm)
        self.stt = build_stt(config.speech.stt)
        self.tts = build_tts(config.speech.tts)
        self.sessions = SessionStore()
        self.agent = AgentLoop(
            self.catalog,
            self.moduleapi,
            self.llm,
            config.budgets,
            system_prompt=config.llm.system_prompt,
            store=self.store,
            bus=self.bus,
            timezone=config.hub.timezone,
        )
        self.scheduler = Scheduler(self.moduleapi, self.bus, config, store=self.store)

        self._stop = asyncio.Event()
        self._server: uvicorn.Server | None = None
        self._background: list[asyncio.Task[None]] = []

    # --- lifecycle ---------------------------------------------------------
    def request_stop(self) -> None:
        self._stop.set()

    def _register_builtins(self) -> None:
        """Insert the synthetic `core` module so its tools appear in the catalog
        and dispatch in process through the same gate as any other module."""
        from .builtins import CORE_MODULE, build_core_module

        module = build_core_module(self.store, self.scheduler)
        self.supervisor.modules[CORE_MODULE] = module
        metrics.set_module_state(CORE_MODULE, module.state.value)

    async def run(self) -> None:
        self.config.hub.state_dir.mkdir(parents=True, exist_ok=True)
        await self.store.open()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.request_stop)

        log.info(
            "hub_starting",
            config=str(self.config_path),
            modules_dir=str(self.config.hub.modules_dir),
            llm_provider=self.config.llm.provider,
        )
        self.supervisor.discover()
        await self.supervisor.start()
        # After start(): the built-in module is always ready and has no process,
        # so it must not be handed to the supervisor's spawn loop.
        self._register_builtins()
        self.scheduler.start()
        await self.scheduler.sync_from_store()
        self._background.append(
            asyncio.create_task(self._persist_module_state(), name="persist-module-state")
        )

        from ..web.app import create_app  # deferred: web imports Runtime for its type hints

        app = create_app(self)
        server_config = uvicorn.Config(
            app,
            host=self.config.web.host,
            port=self.config.web.port,
            log_config=None,  # structlog owns the log format
            lifespan="on",
        )
        self._server = uvicorn.Server(server_config)
        server_task = asyncio.create_task(self._server.serve(), name="web")
        log.info("hub_ready", bind=f"{self.config.web.host}:{self.config.web.port}")

        try:
            await self._stop.wait()
        finally:
            await self._shutdown(server_task)

    async def _shutdown(self, server_task: asyncio.Task[None]) -> None:
        log.info("hub_stopping")
        if self._server is not None:
            self._server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await server_task

        with contextlib.suppress(Exception):
            self.scheduler.stop()

        for task in self._background:
            task.cancel()
        for task in self._background:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._background.clear()

        await self.supervisor.stop()

        for adapter in (self.llm, self.stt, self.tts):
            await _aclose(adapter)

        self.bus.close()
        await self.store.close()
        log.info("hub_stopped")

    async def _persist_module_state(self) -> None:
        """Mirror state transitions into the database, so "why was the lock
        module down last night" has an answer after a restart."""
        sub = self.bus.subscribe("module.state_changed")
        try:
            async for event in sub.events():
                try:
                    await self.store.save_module_state(
                        event["module"], event["state"], event.get("last_error")
                    )
                except Exception as e:  # pragma: no cover - persistence is best effort
                    log.warning("module_state_persist_failed", error=str(e))
        except asyncio.CancelledError:
            pass
        finally:
            self.bus.unsubscribe(sub)


async def serve(config: Config, config_path: Path | None = None) -> None:
    """Configure logging from the config and run the hub until it is stopped."""
    configure_logging(config.hub.log_level, config.hub.log_format)
    await Runtime(config, config_path).run()


async def _aclose(adapter: Any) -> None:
    closer = getattr(adapter, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception as e:  # pragma: no cover - shutdown must not fail on a close
        log.warning("adapter_close_failed", adapter=type(adapter).__name__, error=str(e))
