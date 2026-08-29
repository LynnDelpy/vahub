"""The scheduler: runtime-editable schedules and the boundaries around them."""

from __future__ import annotations

from pathlib import Path

import pytest

from vahub.config.models import Config

pytestmark = pytest.mark.integration


class _DummyAPI:
    """Stands in for the ModuleAPI. `declared` is what each module's own manifest
    claims, which is a different question from what the policy rule says: the
    scheduler has to consider both."""

    def __init__(self, declared: dict[str, str] | None = None) -> None:
        self.declared = declared or {}

    async def call(self, **_: object) -> dict:
        return {"ok": True}

    def effective_class(self, module: str, tool: str) -> str | None:
        return self.declared.get(f"{module}.{tool}")


def _config(state_dir: Path, modules_dir: Path) -> Config:
    return Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "llm": {"provider": "mock"},
            "policy": {
                "default": "deny",
                "principals": {
                    "agent": {"confirm": ["destructive"], "deny": []},
                    "scheduler": {"confirm": [], "deny": []},
                },
                "rules": {
                    "time.now": {"class": "read"},
                    "door.unlock": {"class": "destructive", "constraints": {"id": {"max_len": 10}}},
                },
            },
        }
    )


@pytest.fixture
async def scheduler(construct, state_dir: Path, modules_dir: Path):
    from vahub.core.bus import EventBus
    from vahub.scheduler import Scheduler
    from vahub.storage.store import Store

    store = Store(state_dir / "vahub.db")
    await store.open()
    sched = Scheduler(_DummyAPI(), EventBus(), _config(state_dir, modules_dir), store=store)
    try:
        yield sched, store
    finally:
        await store.close()


async def test_add_list_toggle_remove(scheduler) -> None:
    sched, store = scheduler
    created = await sched.add_dynamic(
        "0 7 * * *", [{"module": "time", "tool": "now", "args": {}}], description="morning"
    )
    assert created["ok"] is True
    sid = created["id"]

    listed = sched.list_schedules()
    entry = next(s for s in listed if s["id"] == sid)
    assert entry["editable"] is True and entry["enabled"] is True

    assert (await sched.set_dynamic_enabled(sid, False))["ok"] is True
    assert (await sched.remove_dynamic(sid))["ok"] is True
    assert not any(s["id"] == sid for s in sched.list_schedules())
    # It is gone from the database too.
    assert await store.list_dyn_schedules() == []


async def test_a_destructive_step_is_refused(scheduler) -> None:
    # A runtime schedule runs unattended as the scheduler, which cannot confirm,
    # so a destructive step must not be schedulable through the API or a tool.
    # This is what stops the agent laundering a confirm-gated action into one
    # that fires with no human.
    sched, _store = scheduler
    step = {"module": "door", "tool": "unlock", "args": {"id": "front"}}
    result = await sched.add_dynamic("0 7 * * *", [step])
    assert result["ok"] is False and result["error"] == "destructive_not_schedulable"


async def test_a_step_the_module_itself_calls_destructive_is_refused(
    state_dir: Path, modules_dir: Path
) -> None:
    """The guard has to read the module's own declaration, not only the policy.

    A tool a module declares destructive, that no rule names (or that a rule
    classes more weakly), used to slip past this check: the rule was the only
    thing consulted, and there was no rule. It could then be scheduled to run
    unattended, which is the exact laundering this refusal exists to stop.
    """
    from vahub.core.bus import EventBus
    from vahub.scheduler import Scheduler
    from vahub.storage.store import Store

    store = Store(state_dir / "declared.db")
    await store.open()
    api = _DummyAPI({"garage.open_everything": "destructive"})
    sched = Scheduler(api, EventBus(), _config(state_dir, modules_dir), store=store)
    try:
        step = {"module": "garage", "tool": "open_everything", "args": {}}
        result = await sched.add_dynamic("0 7 * * *", [step])
        assert result["ok"] is False and result["error"] == "destructive_not_schedulable"
        # and nothing was written: a refusal that still persisted the schedule
        # would be worse than no refusal at all
        assert await store.list_dyn_schedules() == []
    finally:
        await store.close()


async def test_a_harmless_step_is_still_schedulable(scheduler) -> None:
    sched, _store = scheduler
    ok = await sched.add_dynamic("0 7 * * *", [{"module": "time", "tool": "now", "args": {}}])
    assert ok["ok"] is True


async def test_bad_cron_is_rejected(scheduler) -> None:
    sched, _store = scheduler
    result = await sched.add_dynamic("not a cron", [{"module": "time", "tool": "now"}])
    assert result["ok"] is False and result["error"] == "bad_cron"


async def test_empty_steps_are_rejected(scheduler) -> None:
    sched, _store = scheduler
    result = await sched.add_dynamic("0 7 * * *", [])
    assert result["ok"] is False and result["error"] == "bad_steps"


async def test_a_file_schedule_is_not_removable_at_runtime(construct, state_dir, modules_dir) -> None:
    from vahub.core.bus import EventBus
    from vahub.scheduler import Scheduler
    from vahub.storage.store import Store

    config = Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "llm": {"provider": "mock"},
            "policy": {"default": "deny", "rules": {"time.now": {"class": "read"}}},
            "schedules": [
                {"id": "morning", "cron": "0 7 * * *", "steps": [{"module": "time", "tool": "now"}]}
            ],
        }
    )
    store = Store(state_dir / "vahub.db")
    await store.open()
    sched = Scheduler(_DummyAPI(), EventBus(), config, store=store)
    try:
        assert (await sched.remove_dynamic("morning"))["error"] == "not_editable"
        assert next(s for s in sched.list_schedules() if s["id"] == "morning")["editable"] is False
    finally:
        await store.close()


# --------------------------------------------------------------------------
# what happens when a routine actually fires
# --------------------------------------------------------------------------
class _RecordingAPI(_DummyAPI):
    """Records every call, and answers with whatever the test queued."""

    def __init__(self, answers: list[dict] | None = None, declared: dict[str, str] | None = None) -> None:
        super().__init__(declared)
        self.calls: list[dict] = []
        self.cancelled: list[str] = []
        self._answers = list(answers or [])

    async def call(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return self._answers.pop(0) if self._answers else {"ok": True}

    async def cancel(self, pending_id: str) -> dict:
        self.cancelled.append(pending_id)
        return {"ok": True}


async def _sched_with(api, state_dir: Path, modules_dir: Path, name: str = "fire.db"):
    from vahub.core.bus import EventBus
    from vahub.scheduler import Scheduler
    from vahub.storage.store import Store

    store = Store(state_dir / name)
    await store.open()
    return Scheduler(api, EventBus(), _config(state_dir, modules_dir), store=store), store


async def test_a_routine_runs_as_the_scheduler_not_as_a_person(state_dir: Path, modules_dir: Path) -> None:
    """The principal decides what the gate allows. A routine that ran as the
    signed-in owner would quietly do more than the scheduler may."""
    api = _RecordingAPI()
    sched, store = await _sched_with(api, state_dir, modules_dir)
    try:
        created = await sched.add_dynamic("0 7 * * *", [{"module": "time", "tool": "now", "args": {}}])
        result = await sched.run_now(created["id"])
        assert result["ok"] is True
        assert len(api.calls) == 1
        assert api.calls[0]["principal"] == "scheduler"
        assert api.calls[0]["module"] == "time" and api.calls[0]["tool"] == "now"
    finally:
        await store.close()


async def test_a_step_cannot_edit_the_arguments_of_the_next_run(state_dir: Path, modules_dir: Path) -> None:
    """The call path is handed a copy. If it were handed the stored dict, a tool
    that mutated its arguments would silently rewrite the routine."""
    api = _RecordingAPI()
    sched, store = await _sched_with(api, state_dir, modules_dir, "args.db")
    try:
        created = await sched.add_dynamic(
            "0 7 * * *", [{"module": "time", "tool": "now", "args": {"zone": "Europe/Zurich"}}]
        )
        await sched.run_now(created["id"])
        api.calls[0]["args"]["zone"] = "somewhere else"      # a badly behaved callee
        await sched.run_now(created["id"])
        assert api.calls[1]["args"] == {"zone": "Europe/Zurich"}
    finally:
        await store.close()


async def test_a_failing_step_stops_the_routine(state_dir: Path, modules_dir: Path) -> None:
    api = _RecordingAPI([{"ok": False, "error": "policy_denied"}])
    sched, store = await _sched_with(api, state_dir, modules_dir, "stop.db")
    try:
        created = await sched.add_dynamic(
            "0 7 * * *",
            [
                {"module": "time", "tool": "now", "args": {}},
                {"module": "notify", "tool": "send", "args": {}},
            ],
        )
        result = await sched.run_now(created["id"])
        assert result["ok"] is False and result["failed_step"] == 0
        assert len(api.calls) == 1, "the second step ran after the first failed"
    finally:
        await store.close()


async def test_a_step_that_needs_confirming_leaves_no_card_behind(state_dir: Path, modules_dir: Path) -> None:
    """Nobody is there to answer. A pending confirmation left sitting could be
    approved later by a person who has lost the context it came from, so the
    routine cancels its own."""
    api = _RecordingAPI([{"ok": False, "error": "confirmation_required", "pending_id": "abc123"}])
    sched, store = await _sched_with(api, state_dir, modules_dir, "confirm.db")
    try:
        created = await sched.add_dynamic("0 7 * * *", [{"module": "time", "tool": "now", "args": {}}])
        result = await sched.run_now(created["id"])
        assert result["ok"] is False
        assert api.cancelled == ["abc123"]
    finally:
        await store.close()


async def test_a_tool_that_raises_is_reported_not_propagated(state_dir: Path, modules_dir: Path) -> None:
    """An exception escaping into the job runner would kill the timed run and
    leave nothing behind explaining why."""

    class _Exploding(_RecordingAPI):
        async def call(self, **kwargs: object) -> dict:
            self.calls.append(kwargs)
            raise RuntimeError("the module fell over")

    api = _Exploding()
    sched, store = await _sched_with(api, state_dir, modules_dir, "raise.db")
    try:
        created = await sched.add_dynamic("0 7 * * *", [{"module": "time", "tool": "now", "args": {}}])
        result = await sched.run_now(created["id"])
        assert result["ok"] is False
        assert result["results"][0]["result"]["error"] == "internal"
    finally:
        await store.close()


async def test_a_routine_does_not_run_on_top_of_itself(state_dir: Path, modules_dir: Path) -> None:
    """A run that takes longer than the interval must not stack up."""
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    class _Slow(_RecordingAPI):
        async def call(self, **kwargs: object) -> dict:
            self.calls.append(kwargs)
            started.set()
            await release.wait()
            return {"ok": True}

    api = _Slow()
    sched, store = await _sched_with(api, state_dir, modules_dir, "overlap.db")
    try:
        created = await sched.add_dynamic("0 7 * * *", [{"module": "time", "tool": "now", "args": {}}])
        first = asyncio.create_task(sched.run_now(created["id"]))
        await asyncio.wait_for(started.wait(), timeout=2)
        second = await sched.run_now(created["id"])          # while the first is still going
        assert second["ok"] is False and second["error"] == "already_running"
        release.set()
        assert (await first)["ok"] is True
        assert len(api.calls) == 1
    finally:
        await store.close()


async def test_a_disabled_routine_refuses_to_run(state_dir: Path, modules_dir: Path) -> None:
    api = _RecordingAPI()
    sched, store = await _sched_with(api, state_dir, modules_dir, "disabled.db")
    try:
        created = await sched.add_dynamic("0 7 * * *", [{"module": "time", "tool": "now", "args": {}}])
        await sched.set_dynamic_enabled(created["id"], False)
        listed = {s["id"]: s for s in sched.list_schedules()}
        assert listed[created["id"]]["enabled"] is False
        assert api.calls == []
    finally:
        await store.close()


async def test_running_something_that_does_not_exist_is_an_answer(state_dir: Path, modules_dir: Path) -> None:
    api = _RecordingAPI()
    sched, store = await _sched_with(api, state_dir, modules_dir, "missing.db")
    try:
        result = await sched.run_now("nosuchschedule")
        assert result["ok"] is False and result["error"] == "unknown_schedule"
    finally:
        await store.close()
