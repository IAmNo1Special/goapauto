"""Tests for cooperative action interruption (design 03), part 2.

Sync non-preemption, async cancellation semantics, hook contract, replan
composition, threading, and backward compatibility.
"""

import asyncio
import logging
import threading
import time

import pytest

from goapauto.models.execution import PlanExecution, PlanInterruptedError
from goapauto.models.goal import Goal
from goapauto.models.goap_planner import PlanExecutionError, Planner
from goapauto.models.worldstate import WorldState

ACTIONS = [
    ("step1", {"s": 0}, {"s": 1}, 1.0),
    ("step2", {"s": 1}, {"s": 2}, 1.0),
    ("step3", {"s": 2}, {"s": 3}, 1.0),
]

HOOK_KWARGS = {
    "reason",
    "source",
    "state",
    "executed_actions",
    "remaining_actions",
    "step_index",
    "plan_length",
    "state_diff",
    "interrupted_at",
}


def make_execution(
    planner: Planner, plan=None, state: WorldState | None = None
) -> PlanExecution:
    return planner.begin_execution(
        state if state is not None else WorldState(s=0),
        plan if plan is not None else ["step1", "step2", "step3"],
    )


@pytest.fixture
def planner() -> Planner:
    return Planner(actions_list=ACTIONS, verbose=False)


@pytest.fixture
def goal() -> Goal:
    return Goal(target_state={"s": 3}, name="reach-three")


class TestSyncNonPreemption:
    def test_handler_interrupt_on_itself_completes_first(self, planner: Planner):
        events = []
        execution = make_execution(planner, plan=["step1", "step2"])

        def handler(state, action):
            events.append("handler-start")
            assert execution.interrupt("self-stop", source="manual") is True
            events.append("handler-end")
            return action.apply(state)

        planner.register_execution_handler("step1", handler)
        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()
        # The handler ran to completion; the interrupt took effect at the
        # next boundary, never inside the handler.
        assert events == ["handler-start", "handler-end"]
        assert exc_info.value.executed_actions == ["step1"]
        assert exc_info.value.next_action == "step2"

    def test_watchdog_interrupt_during_sync_handler_waits_for_boundary(
        self, planner: Planner
    ):
        completed = []

        def slow_handler(state, action):
            time.sleep(0.3)
            completed.append(action.name)
            return action.apply(state)

        planner.register_execution_handler("step1", slow_handler)
        execution = make_execution(planner, plan=["step1", "step2"])

        def watchdog():
            time.sleep(0.05)
            assert execution.interrupt("watchdog stop", source="watchdog") is True

        thread = threading.Thread(target=watchdog)
        thread.start()
        started = time.monotonic()
        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()
        thread.join()
        # The in-flight handler was not preempted: it ran to completion and
        # the interrupt was honored at the next action boundary.
        assert completed == ["step1"]
        assert time.monotonic() - started >= 0.3
        assert exc_info.value.executed_actions == ["step1"]
        assert exc_info.value.source == "watchdog"


class TestAsyncExecution:
    async def test_arun_completes_with_async_handlers(self, planner: Planner):
        async def handler(state, action):
            await asyncio.sleep(0)
            return action.apply(state)

        planner.register_execution_handler("step1", handler)
        planner.register_execution_handler("step2", handler)
        execution = make_execution(planner)
        final = await execution.arun()
        assert final.get_state() == {"s": 3}
        assert execution.is_complete is True
        assert execution.executed_actions == ("step1", "step2", "step3")

    async def test_arun_uses_async_apply_without_handlers(self, planner: Planner):
        execution = make_execution(planner, plan=["step1"])
        final = await execution.arun()
        assert final.get_state() == {"s": 1}

    def test_run_rejects_async_handler(self, planner: Planner):
        async def handler(state, action):
            return state

        planner.register_execution_handler("step1", handler)
        execution = make_execution(planner, plan=["step1"])
        with pytest.raises(TypeError, match="Async execution handler"):
            execution.run()
        assert execution.is_complete is False
        assert execution.is_interrupted is False

    async def test_arun_interrupt_from_another_task(self, planner: Planner):
        started = asyncio.Event()

        async def slow_handler(state, action):
            started.set()
            # No cancellation is injected: the flag waits for this await to
            # finish and is honored at the next boundary.
            await asyncio.sleep(0.5)
            return action.apply(state)

        planner.register_execution_handler("step1", slow_handler)
        execution = make_execution(planner)

        async def interrupter():
            await started.wait()
            assert execution.interrupt("other task", source="watchdog") is True

        interrupter_task = asyncio.create_task(interrupter())
        with pytest.raises(PlanInterruptedError) as exc_info:
            await execution.arun()
        await interrupter_task
        assert exc_info.value.executed_actions == ["step1"]
        assert exc_info.value.source == "watchdog"

    async def test_arun_interrupt_between_steps(self, planner: Planner):
        execution = make_execution(planner)

        def on_complete(*, action, state) -> None:
            if action.name == "step2":
                execution.interrupt("stop", source="budget")

        planner.register_hook("on_action_complete", on_complete)
        with pytest.raises(PlanInterruptedError) as exc_info:
            await execution.arun()
        err = exc_info.value
        assert err.executed_actions == ["step1", "step2"]
        assert err.source == "budget"
        assert err.state_diff == {"s": (0, 2)}

    async def test_arun_cancel_marks_interrupted(self, planner: Planner):
        fired = []
        started = asyncio.Event()

        async def slow_handler(state, action):
            started.set()
            await asyncio.sleep(30)
            return state

        planner.register_execution_handler("step1", slow_handler)
        planner.register_hook("on_execution_interrupted", lambda **kw: fired.append(kw))
        execution = make_execution(planner)

        task = asyncio.create_task(execution.arun())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert execution.is_interrupted is True
        assert execution.is_complete is False
        assert execution.interrupt_source == "cancellation"
        assert execution.interrupt_reason == "task cancelled"
        assert len(fired) == 1
        assert fired[0]["source"] == "cancellation"
        assert fired[0]["reason"] == "task cancelled"
        assert fired[0]["executed_actions"] == []
        assert fired[0]["step_index"] == 0
        assert set(fired[0]) == HOOK_KWARGS

    async def test_arun_cancel_keeps_first_recorded_reason(self, planner: Planner):
        interrupted = asyncio.Event()
        planner.register_hook(
            "on_execution_interrupted", lambda **kw: interrupted.set()
        )
        execution = make_execution(planner)
        execution.interrupt("manual stop", source="manual")

        task = asyncio.create_task(execution.arun())
        await interrupted.wait()
        # The pre-loop boundary honored the manual interrupt first; a later
        # cancellation cannot overwrite the recorded reason.
        task.cancel()
        with pytest.raises(PlanInterruptedError):
            await task
        assert execution.interrupt_reason == "manual stop"
        assert execution.interrupt_source == "manual"

    async def test_arun_cancel_during_handler_keeps_manual_reason(
        self, planner: Planner
    ):
        started = asyncio.Event()

        async def slow_handler(state, action):
            started.set()
            await asyncio.sleep(30)
            return state

        planner.register_execution_handler("step1", slow_handler)
        execution = make_execution(planner)

        task = asyncio.create_task(execution.arun())
        await started.wait()
        # Manual interrupt recorded while the handler is awaited...
        assert execution.interrupt("manual stop", source="manual") is True
        # ...then the task is cancelled before the next boundary.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # First-wins: the manual reason stands, cancellation only marks.
        assert execution.interrupt_reason == "manual stop"
        assert execution.interrupt_source == "manual"
        assert execution.is_interrupted is True

    async def test_cancellation_hook_error_cannot_supersede(
        self, planner: Planner, caplog: pytest.LogCaptureFixture
    ):
        started = asyncio.Event()

        async def slow_handler(state, action):
            started.set()
            await asyncio.sleep(30)
            return state

        def bad_hook(**kwargs):
            raise RuntimeError("telemetry down")

        planner.register_execution_handler("step1", slow_handler)
        planner.register_hook("on_execution_interrupted", bad_hook)
        execution = make_execution(planner)

        task = asyncio.create_task(execution.arun())
        await started.wait()
        task.cancel()
        with caplog.at_level(logging.ERROR, logger="goapauto.models.goap_planner"):
            with pytest.raises(asyncio.CancelledError):
                await task
        # Cancellation purity: the hook error is captured and logged, the
        # CancelledError still propagates unchanged.
        assert execution.is_interrupted is True
        assert any(
            "on_execution_interrupted" in record.message for record in caplog.records
        )

    async def test_handler_swallowing_cancelled_error_continues(self, planner: Planner):
        fired = []
        started = asyncio.Event()

        async def sneaky_handler(state, action):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                pass  # hostile: suppresses the injected cancellation
            return action.apply(state)

        planner.register_execution_handler("step1", sneaky_handler)
        planner.register_hook("on_execution_interrupted", lambda **kw: fired.append(kw))
        execution = make_execution(planner, plan=["step1"])

        task = asyncio.create_task(execution.arun())
        await started.wait()
        task.cancel()
        # The handler chose to suppress cancellation: arun continues as if
        # nothing happened -- no marking, no hook. Documented limitation.
        final = await task
        assert final.get_state() == {"s": 1}
        assert execution.is_complete is True
        assert execution.is_interrupted is False
        assert fired == []

    async def test_interrupt_during_awaited_handler_honored_after(
        self, planner: Planner
    ):
        entered = asyncio.Event()
        release = asyncio.Event()
        finished_handler = []

        async def gated_handler(state, action):
            entered.set()
            await release.wait()
            finished_handler.append(action.name)
            return action.apply(state)

        planner.register_execution_handler("step1", gated_handler)
        execution = make_execution(planner, plan=["step1", "step2"])

        task = asyncio.create_task(execution.arun())
        await entered.wait()
        # No cancellation is injected into the in-flight handler: the flag
        # waits and is honored at the next boundary after it completes.
        assert execution.interrupt("stop", source="watchdog") is True
        release.set()
        with pytest.raises(PlanInterruptedError) as exc_info:
            await task
        assert finished_handler == ["step1"]
        assert exc_info.value.executed_actions == ["step1"]

    def test_arun_without_running_loop_raises(self, planner: Planner):
        execution = make_execution(planner, plan=["step1"])
        coro = execution.arun()
        try:
            with pytest.raises(RuntimeError):
                coro.send(None)
        finally:
            coro.close()
        # No state was mutated by the stillborn call.
        assert execution.is_running is False
        assert execution.is_complete is False

    async def test_arun_unknown_action_name_raises_key_error(self, planner: Planner):
        execution = planner.begin_execution(WorldState(s=0), ["nope"])
        with pytest.raises(KeyError):
            await execution.arun()
        assert execution.is_interrupted is False
        assert execution.is_complete is False

    async def test_arun_inapplicable_action_fails(self, planner: Planner):
        failed = []
        planner.register_hook("on_execution_failed", lambda **kw: failed.append(kw))
        execution = planner.begin_execution(WorldState(s=99), ["step1"])
        with pytest.raises(PlanExecutionError):
            await execution.arun()
        assert len(failed) == 1
        assert execution.is_interrupted is False

    async def test_arun_supports_sync_handlers(self, planner: Planner):
        planner.register_execution_handler("step1", lambda s, a: a.apply(s))
        execution = make_execution(planner, plan=["step1"])
        final = await execution.arun()
        assert final.get_state() == {"s": 1}

    async def test_arun_handler_exception_marks_failed(self, planner: Planner):
        action_failed = []
        execution_failed = []
        planner.register_hook("on_action_failed", lambda **kw: action_failed.append(kw))
        planner.register_hook(
            "on_execution_failed", lambda **kw: execution_failed.append(kw)
        )

        async def boom(state, action):
            raise RuntimeError("async boom")

        planner.register_execution_handler("step2", boom)
        execution = make_execution(planner)
        with pytest.raises(RuntimeError, match="async boom"):
            await execution.arun()
        assert len(action_failed) == 1
        assert len(execution_failed) == 1
        assert execution.is_complete is False
        assert execution.is_interrupted is False

    async def test_arun_handler_plan_execution_error_skips_failure_hooks(
        self, planner: Planner
    ):
        failed = []
        planner.register_hook("on_execution_failed", lambda **kw: failed.append(kw))

        async def plan_failure(state, action):
            raise PlanExecutionError("handler-level failure")

        planner.register_execution_handler("step1", plan_failure)
        execution = make_execution(planner, plan=["step1"])
        with pytest.raises(PlanExecutionError):
            await execution.arun()
        # Parity with async_execute_plan: a handler raising PlanExecutionError
        # does not re-fire the failure hooks.
        assert failed == []
        assert execution.is_complete is False

    async def test_arun_interrupt_from_last_action_post_loop(self, planner: Planner):
        completed = []
        planner.register_hook(
            "on_execution_complete", lambda **kw: completed.append(kw)
        )
        execution = make_execution(planner)

        def on_complete(*, action, state) -> None:
            if action.name == "step3":
                execution.interrupt("done early", source="sensor")

        planner.register_hook("on_action_complete", on_complete)
        with pytest.raises(PlanInterruptedError) as exc_info:
            await execution.arun()
        err = exc_info.value
        assert err.remaining_actions == []
        assert err.next_action is None
        assert completed == []
        assert execution.is_interrupted is True


class TestHookContract:
    def test_hook_fires_exactly_once_with_exact_kwargs(self, planner: Planner):
        fired = []
        execution = make_execution(planner, plan=["step1", "step2"])
        planner.register_hook("on_execution_interrupted", lambda **kw: fired.append(kw))
        execution.interrupt("enemy_sighted", source="sensor")
        with pytest.raises(PlanInterruptedError):
            execution.run()

        assert len(fired) == 1
        kwargs = fired[0]
        assert set(kwargs) == HOOK_KWARGS
        assert kwargs["reason"] == "enemy_sighted"
        assert kwargs["source"] == "sensor"
        assert isinstance(kwargs["state"], WorldState)
        assert kwargs["executed_actions"] == []
        assert kwargs["remaining_actions"] == ["step1", "step2"]
        assert kwargs["step_index"] == 0
        assert kwargs["plan_length"] == 2
        assert kwargs["state_diff"] == {}
        assert isinstance(kwargs["interrupted_at"], float)

    def test_hook_mutating_kwargs_cannot_corrupt_error_payload(self, planner: Planner):
        def meddling_hook(**kwargs):
            kwargs["executed_actions"].append("HACK")
            kwargs["remaining_actions"].append("HACK")
            kwargs["state_diff"]["HACK"] = (0, 0)

        planner.register_hook("on_execution_interrupted", meddling_hook)
        execution = make_execution(planner, plan=["step1"])
        execution.interrupt("stop", source="manual")
        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()
        err = exc_info.value
        assert err.executed_actions == []
        assert err.remaining_actions == ["step1"]
        assert err.state_diff == {}
        assert err.next_action == "step1"

    def test_hook_state_is_same_object_as_err_state(self, planner: Planner):
        fired = []
        execution = make_execution(planner, plan=["step1"])
        planner.register_hook("on_execution_interrupted", lambda **kw: fired.append(kw))
        execution.interrupt("stop", source="manual")
        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()
        assert fired[0]["state"] is exc_info.value.state

    def test_hook_exact_ordering(self, planner: Planner):
        events = []
        planner.register_hook(
            "on_action_start", lambda **kw: events.append(("start", kw["action"].name))
        )
        planner.register_hook(
            "on_action_complete",
            lambda **kw: events.append(("complete", kw["action"].name)),
        )
        planner.register_hook(
            "on_execution_interrupted", lambda **kw: events.append(("interrupted",))
        )
        planner.register_hook(
            "on_execution_complete", lambda **kw: events.append(("completed",))
        )
        planner.register_hook(
            "on_execution_failed", lambda **kw: events.append(("failed",))
        )

        execution = make_execution(planner, plan=["step1", "step2", "step3"])

        def stop_after_two(*, action, state) -> None:
            if action.name == "step2":
                execution.interrupt("stop", source="manual")

        planner.register_hook("on_action_complete", stop_after_two)
        with pytest.raises(PlanInterruptedError):
            execution.run()

        # The never-started action gets no on_action_start; the interrupt
        # event fires exactly once, last, with nothing after it.
        assert events == [
            ("start", "step1"),
            ("complete", "step1"),
            ("start", "step2"),
            ("complete", "step2"),
            ("interrupted",),
        ]

    def test_hook_raising_on_normal_path_supersedes(self, planner: Planner):
        def bad_hook(**kwargs):
            raise RuntimeError("hook boom")

        planner.register_hook("on_execution_interrupted", bad_hook)
        execution = make_execution(planner, plan=["step1"])
        execution.interrupt("stop", source="manual")
        with pytest.raises(RuntimeError, match="hook boom"):
            execution.run()
        # The run did stop because of the interrupt: terminal stays
        # interrupted even though the reporting hook failed.
        assert execution.is_interrupted is True
        assert execution.is_complete is False

    def test_no_interrupt_hook_on_normal_completion(self, planner: Planner):
        fired = []
        planner.register_hook("on_execution_interrupted", lambda **kw: fired.append(kw))
        make_execution(planner, plan=["step1"]).run()
        assert fired == []

    def test_no_interrupt_hook_on_failure(self, planner: Planner):
        fired = []
        planner.register_hook("on_execution_interrupted", lambda **kw: fired.append(kw))
        execution = planner.begin_execution(WorldState(s=99), ["step1"])
        with pytest.raises(PlanExecutionError):
            execution.run()
        assert fired == []


class TestReplanComposition:
    def test_replan_from_interruption_reaches_goal(self, planner: Planner, goal: Goal):
        execution = planner.begin_execution(
            WorldState(s=0), ["step1", "step2", "step3"], goal
        )

        def stop_after_two(*, action, state) -> None:
            if action.name == "step2":
                execution.interrupt("sensor says stop", source="sensor")

        planner.register_hook("on_action_complete", stop_after_two)
        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()

        err = exc_info.value
        new_result = planner.continue_plan(err.state, goal, list(err.executed_actions))
        assert new_result.plan == ["step3"]
        final = planner.execute_plan(err.state, new_result.plan, goal)
        assert goal.is_satisfied(final)

    def test_action_object_steps_use_names_for_prefix_strip(
        self, planner: Planner, goal: Goal
    ):
        from goapauto.models.actions import Action

        objs = [
            Action(name="step1", preconditions={"s": 0}, effects={"s": 1}, cost=1.0),
            Action(name="step2", preconditions={"s": 1}, effects={"s": 2}, cost=1.0),
            Action(name="step3", preconditions={"s": 2}, effects={"s": 3}, cost=1.0),
        ]
        execution = planner.begin_execution(WorldState(s=0), objs, goal)

        def stop_after_one(*, action, state) -> None:
            if action.name == "step1":
                execution.interrupt("stop", source="manual")

        planner.register_hook("on_action_complete", stop_after_one)
        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()

        err = exc_info.value
        assert err.executed_actions == ["step1"]
        assert err.remaining_actions == ["step2", "step3"]
        assert all(isinstance(n, str) for n in err.executed_actions)
        # continue_plan's prefix strip works on the name strings: no replay.
        new_result = planner.continue_plan(err.state, goal, list(err.executed_actions))
        assert new_result.plan == ["step2", "step3"]


class TestThreading:
    def test_concurrent_interrupt_first_wins(self, planner: Planner):
        execution = make_execution(planner, plan=["step1", "step2"])
        barrier = threading.Barrier(2)
        outcomes = {}
        reasons = {"t1": "reason-1", "t2": "reason-2"}

        def racer(name):
            barrier.wait(timeout=5)
            outcomes[name] = execution.interrupt(reasons[name], source="watchdog")

        threads = [threading.Thread(target=racer, args=(n,)) for n in ("t1", "t2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(outcomes.values()) == [False, True]
        winner = "t1" if outcomes["t1"] else "t2"
        assert execution.interrupt_reason == reasons[winner]
        assert execution.interrupt_source == "watchdog"

    def test_cross_thread_inspection_is_snapshot_safe(self, planner: Planner):
        gate = threading.Event()

        def gated_handler(state, action):
            gate.wait(timeout=5)
            return action.apply(state)

        planner.register_execution_handler("step1", gated_handler)
        planner.register_execution_handler("step2", gated_handler)
        execution = make_execution(planner, plan=["step1", "step2"])

        errors = []
        snapshots = []

        def watchdog():
            time.sleep(0.05)
            assert execution.interrupt("watch", source="watchdog") is True
            gate.set()
            for _ in range(200):
                try:
                    snapshots.append(
                        (
                            execution.executed_actions,
                            execution.remaining_actions,
                            execution.state.get_state().get("s"),
                            execution.is_running,
                            execution.is_interrupted,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - any failure is a bug
                    errors.append(exc)

        thread = threading.Thread(target=watchdog)
        thread.start()
        with pytest.raises(PlanInterruptedError):
            execution.run()
        thread.join()

        assert errors == []
        assert snapshots
        for executed, remaining, _s, _running, _interrupted in snapshots:
            assert isinstance(executed, tuple)
            assert isinstance(remaining, tuple)
            # Snapshots are self-consistent: executed + remaining == full plan.
            assert len(executed) + len(remaining) == 2
            assert tuple(executed) + tuple(remaining) == ("step1", "step2")


class TestBackwardCompatibility:
    def test_execute_plan_never_raises_interrupted(self, planner: Planner):
        final = planner.execute_plan(WorldState(s=0), ["step1", "step2", "step3"])
        assert final.get_state() == {"s": 3}

    async def test_async_execute_plan_unchanged(self, planner: Planner):
        final = await planner.async_execute_plan(WorldState(s=0), ["step1"])
        assert final.get_state() == {"s": 1}

    def test_execute_plan_failure_type_unchanged(self, planner: Planner):
        with pytest.raises(PlanExecutionError):
            planner.execute_plan(WorldState(s=99), ["step1"])
