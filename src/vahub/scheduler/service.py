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
import uuid
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config.models import Config, Schedule, ScheduleStep

if TYPE_CHECKING:
    from ..core.bus import EventBus
    from ..core.moduleapi import ModuleAPI
    from ..storage.store import Store

log = structlog.get_logger("vahub.scheduler")

# The principal every scheduled step acts as. It must exist in policy.principals
# or the gate falls back to the default (deny), which is the safe direction.
PRINCIPAL = "scheduler"

# A routine that missed its slot by more than this is dropped rather than run
# late: a "good morning, opening the blinds" routine at 14:00 is not useful.
MISFIRE_GRACE_S = 30


class Scheduler:
    """Runs the routines declared in `config.schedules`."""

    def __init__(
        self, moduleapi: ModuleAPI, bus: EventBus, config: Config, store: Store | None = None
    ) -> None:
        self._api = moduleapi
        self._bus = bus
        self._store = store
        self._policy = config.policy
        self._timezone = _resolve_timezone(config.hub.timezone)
        self._schedules: dict[str, Schedule] = {s.id: s for s in config.schedules}
        self._locks: dict[str, asyncio.Lock] = {sid: asyncio.Lock() for sid in self._schedules}
        # Which ids came from the database rather than the config file. Only these
        # can be edited or removed at runtime; a file schedule is the operator's.
        self._dynamic: set[str] = set()
        self._sched = AsyncIOScheduler(timezone=self._timezone)
        self._stopping = False

    def _register(self, entry: Schedule) -> None:
        """Add or replace one APScheduler job for an enabled schedule."""
        if not entry.enabled:
            return
        try:
            trigger = CronTrigger.from_crontab(entry.cron, timezone=self._timezone)
        except ValueError as e:
            log.error("bad_cron", schedule=entry.id, cron=entry.cron, error=str(e))
            return
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
            self._register(entry)
        self._sched.start()

    async def sync_from_store(self) -> None:
        """Load the runtime-created schedules from the database and register the
        enabled ones. Called once after start(); the ones created later go in
        through add_dynamic()."""
        if self._store is None:
            return
        for row in await self._store.list_dyn_schedules():
            try:
                entry = _row_to_schedule(row)
            except Exception as e:
                log.error("bad_dyn_schedule", id=row.get("id"), error=str(e))
                continue
            self._schedules[entry.id] = entry
            self._dynamic.add(entry.id)
            self._locks.setdefault(entry.id, asyncio.Lock())
            if self._sched.running:
                self._register(entry)

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
                    "steps": [
                        {"module": s.module, "tool": s.tool, "args": dict(s.args)} for s in entry.steps
                    ],
                    "next_run": _next_run(job),
                    "running": bool(lock and lock.locked()),
                    # Only a runtime-created schedule may be edited or removed
                    # through the API or a tool; a file schedule is the operator's.
                    "editable": sid in self._dynamic,
                }
            )
        return out

    # --- runtime editing (DB-backed schedules) -----------------------------
    async def add_dynamic(
        self,
        cron: str,
        steps: list[dict[str, Any]],
        *,
        description: str | None = None,
        created_by: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Create a schedule at runtime, persisted so it survives a restart. The
        steps still run as principal `scheduler`, so this cannot schedule anything
        the scheduler is not already allowed to do."""
        if self._store is None:
            return {"ok": False, "error": "no_store"}
        try:
            CronTrigger.from_crontab(cron, timezone=self._timezone)
        except ValueError as e:
            return {"ok": False, "error": "bad_cron", "detail": str(e)}
        try:
            parsed = [ScheduleStep.model_validate(s) for s in steps]
        except Exception as e:
            return {"ok": False, "error": "bad_steps", "detail": str(e)}
        if not parsed:
            return {"ok": False, "error": "bad_steps", "detail": "at least one step is required"}
        # A schedule runs unattended as the scheduler, which cannot answer a
        # confirmation. So a runtime-created schedule (from the UI or the
        # assistant) must not contain a destructive step: otherwise the agent
        # could launder an action it would have to get confirmed into one that
        # fires with no human. A deliberately scheduled destructive action still
        # belongs in the config file, which is the trusted boundary.
        for step in parsed:
            rule = self._policy.rules.get(f"{step.module}.{step.tool}")
            if rule is not None and rule.cls == "destructive":
                return {
                    "ok": False,
                    "error": "destructive_not_schedulable",
                    "detail": (
                        f"{step.module}.{step.tool} is destructive; schedule it in vahub.yaml "
                        "if you really mean to run it unattended"
                    ),
                }

        sid = f"dyn-{uuid.uuid4().hex[:12]}"
        await self._store.add_dyn_schedule(
            sid,
            cron,
            [s.model_dump() for s in parsed],
            description=description,
            created_by=created_by,
            enabled=enabled,
        )
        entry = Schedule(id=sid, cron=cron, enabled=enabled, steps=parsed)
        self._schedules[sid] = entry
        self._dynamic.add(sid)
        self._locks.setdefault(sid, asyncio.Lock())
        if self._sched.running and enabled:
            self._register(entry)
        return {"ok": True, "id": sid}

    async def remove_dynamic(self, schedule_id: str) -> dict[str, Any]:
        if schedule_id not in self._dynamic:
            return {"ok": False, "error": "not_editable", "detail": schedule_id}
        if self._store is not None:
            await self._store.delete_dyn_schedule(schedule_id)
        self._unregister(schedule_id)
        self._schedules.pop(schedule_id, None)
        self._dynamic.discard(schedule_id)
        return {"ok": True, "id": schedule_id}

    async def set_dynamic_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        if schedule_id not in self._dynamic:
            return {"ok": False, "error": "not_editable", "detail": schedule_id}
        entry = self._schedules.get(schedule_id)
        if entry is None:
            return {"ok": False, "error": "unknown_schedule", "detail": schedule_id}
        if self._store is not None:
            await self._store.set_dyn_schedule_enabled(schedule_id, enabled)
        updated = entry.model_copy(update={"enabled": enabled})
        self._schedules[schedule_id] = updated
        if enabled:
            self._register(updated)
        else:
            self._unregister(schedule_id)
        return {"ok": True, "id": schedule_id, "enabled": enabled}

    def _unregister(self, schedule_id: str) -> None:
        if self._sched.running and self._sched.get_job(schedule_id) is not None:
            self._sched.remove_job(schedule_id)

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

            results.append({"step": index, "module": step.module, "tool": step.tool, "result": result})
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


def _row_to_schedule(row: dict[str, Any]) -> Schedule:
    """Build a Schedule from a stored row, reusing the same model the file
    schedules use so execution is identical."""
    steps = [ScheduleStep.model_validate(s) for s in (row.get("steps") or [])]
    return Schedule(
        id=str(row["id"]),
        cron=str(row["cron"]),
        enabled=bool(row.get("enabled", True)),
        steps=steps,
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
