"""Deterministic routines: the morning routine, without a model in the loop.

Steps run through the module API with principal="scheduler", so the policy gate
still authorizes every call. The scheduler is a separate principal from the
agent on purpose: it may act unattended, which is exactly why it should be
allowed less than a person standing at the console.

Three rules the implementation exists to enforce:

* Overlap is skipped, never queued. A routine still running when its next tick
  arrives is left alone; queueing would turn a slow module into a backlog that
  fires the whole routine repeatedly once it recovers.
* A failing step aborts the routine. Steps are ordered for a reason, and the
  failure is published so something notices.
* The timezone is explicit and comes from the config, so a daylight-saving jump
  moves the routine predictably instead of following the machine's locale.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config.models import Config, Schedule

if TYPE_CHECKING:
    from ..core.bus import EventBus
    from ..core.moduleapi import ModuleAPI

log = structlog.get_logger("vahub.scheduler")

# The principal every scheduled step acts as. It must exist in policy.principals
# or the gate falls back to the default (deny), which is the safe direction.
PRINCIPAL = "scheduler"

# A routine that missed its slot by more than this is dropped rather than run
# late: a "good morning, opening the blinds" routine at 14:00 is not useful.
MISFIRE_GRACE_S = 30


class Scheduler:
    """Runs the routines declared in `config.schedules`."""

    def __init__(self, moduleapi: ModuleAPI, bus: EventBus, config: Config) -> None:
        self._api = moduleapi
        self._bus = bus
        self._timezone = _resolve_timezone(config.hub.timezone)
        self._schedules: dict[str, Schedule] = {s.id: s for s in config.schedules}
        self._locks: dict[str, asyncio.Lock] = {sid: asyncio.Lock() for sid in self._schedules}
        self._sched = AsyncIOScheduler(timezone=self._timezone)
        self._stopping = False

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Register the enabled routines and start ticking.

        Call this from the loop that will run the jobs: APScheduler binds to the
        running event loop here, not at construction time.
        """
        if self._sched.running:
            return
        self._stopping = False
        for entry in self._schedules.values():
            if not entry.enabled:
                log.info("schedule_disabled", schedule=entry.id)
                continue
            try:
                trigger = CronTrigger.from_crontab(entry.cron, timezone=self._timezone)
            except ValueError as e:
                # One unparseable cron expression must not cost the other routines.
                log.error("bad_cron", schedule=entry.id, cron=entry.cron, error=str(e))
                continue
            self._sched.add_job(
                self.run_schedule,
                trigger,
                args=[entry.id],
                id=entry.id,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=MISFIRE_GRACE_S,
                replace_existing=True,
            )
            log.info("schedule_registered", schedule=entry.id, cron=entry.cron, steps=len(entry.steps))
        self._sched.start()

    def stop(self) -> None:
        # APScheduler defers the actual shutdown to the event loop, so `running`
        # is still true immediately after the call. Without our own flag a second
        # stop() (a failed startup unwinding into teardown) queues a second
        # shutdown that raises inside a loop callback, where nobody catches it.
        if self._stopping or not self._sched.running:
            return
        self._stopping = True
        # wait=False: teardown must not block on a routine stuck in a slow module.
        self._sched.shutdown(wait=False)

    # --- introspection -----------------------------------------------------
    def list_schedules(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sid, entry in self._schedules.items():
            job = self._sched.get_job(sid) if self._sched.running else None
            lock = self._locks.get(sid)
            out.append(
                {
                    "id": sid,
                    "cron": entry.cron,
                    "enabled": entry.enabled,
                    "steps": len(entry.steps),
                    "next_run": _next_run(job),
                    "running": bool(lock and lock.locked()),
                }
            )
        return out

    # --- execution ---------------------------------------------------------
    async def run_now(self, schedule_id: str) -> dict[str, Any]:
        """Run a routine immediately. Same path, same principal, same gate as a
        timed run, so "test it now" cannot do more than the cron entry can."""
        return await self.run_schedule(schedule_id)

    async def run_schedule(self, schedule_id: str) -> dict[str, Any]:
        entry = self._schedules.get(schedule_id)
        if entry is None:
            return {"ok": False, "error": "unknown_schedule", "detail": schedule_id}

        lock = self._locks.setdefault(schedule_id, asyncio.Lock())
        # Checking before acquiring is safe because nothing awaits in between.
        # APScheduler's max_instances only covers timed runs; this also covers a
        # manual run_now landing on top of one.
        if lock.locked():
            log.warning("schedule_overlap_skipped", schedule=schedule_id)
            return {"ok": False, "error": "already_running", "detail": schedule_id}
        async with lock:
            return await self._run_steps(entry)

    async def _run_steps(self, entry: Schedule) -> dict[str, Any]:
        started = time.monotonic()
        log.info("schedule_fired", schedule=entry.id, steps=len(entry.steps))
        results: list[dict[str, Any]] = []

        for index, step in enumerate(entry.steps):
            try:
                result = await self._api.call(
                    module=step.module,
                    tool=step.tool,
                    # A copy: the call path must not be able to mutate the
                    # arguments held by the loaded config and change the next run.
                    args=dict(step.args),
                    timeout_s=step.timeout_s,
                    principal=PRINCIPAL,
                )
            except Exception as e:
                # The call path is supposed to return errors rather than raise.
                # If it ever does raise, the routine reports it instead of the
                # exception escaping into APScheduler's job runner.
                log.error("schedule_step_raised", schedule=entry.id, step=index, error=str(e))
                result = {"ok": False, "error": "internal", "detail": str(e)}
            if not isinstance(result, dict):
                result = {"ok": False, "error": "bad_result"}

            results.append(
                {"step": index, "module": step.module, "tool": step.tool, "result": result}
            )
            if not result.get("ok"):
                log.warning(
                    "schedule_step_failed",
                    schedule=entry.id,
                    step=index,
                    module=step.module,
                    tool=step.tool,
                    error=str(result.get("error")),
                )
                self._publish(entry.id, ok=False, failed_step=index, results=results, started=started)
                return {"ok": False, "schedule": entry.id, "failed_step": index, "results": results}

        self._publish(entry.id, ok=True, failed_step=None, results=results, started=started)
        return {"ok": True, "schedule": entry.id, "results": results}

    def _publish(
        self,
        schedule_id: str,
        *,
        ok: bool,
        failed_step: int | None,
        results: list[dict[str, Any]],
        started: float,
    ) -> None:
        # The event carries a summary, not the module payloads: bus queues are
        # bounded, and a tool that returns a megabyte of text would otherwise
        # push every other event out of a subscriber's queue.
        summary = [
            {
                "step": item["step"],
                "module": item["module"],
                "tool": item["tool"],
                "ok": bool(item["result"].get("ok")),
                "error": item["result"].get("error"),
            }
            for item in results
        ]
        self._bus.publish(
            "schedule.fired",
            {
                "schedule": schedule_id,
                "ok": ok,
                "failed_step": failed_step,
                "steps": summary,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )


def _next_run(job: Any) -> str | None:
    # A job that has not been scheduled yet has no next_run_time attribute at all.
    nrt = getattr(job, "next_run_time", None)
    return nrt.isoformat() if nrt else None


def _resolve_timezone(name: str) -> str:
    """Validate the configured timezone, falling back to UTC.

    A typo in the timezone should be loud but must not stop the rest of the hub
    from running: the lights still work without the morning routine.
    """
    try:
        ZoneInfo(name)
    except Exception as e:
        log.error("unknown_timezone", timezone=name, error=str(e), fallback="UTC")
        return "UTC"
    return name
