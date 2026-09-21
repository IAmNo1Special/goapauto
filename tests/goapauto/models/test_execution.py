"""Tests for cooperative action interruption (design 03).

Covers the ``PlanExecution`` handle, per-action boundary interruption checks,
``interrupt(reason, source=...)``, the ``on_action_complete`` closure seam,
interruption telemetry/diagnostics payloads, and the producer contract for
design 06.
"""

import pytest

import goapauto
from goapauto.models.action_provider import ActionProvider
from goapauto.models.actions import Action
from goapauto.models.execution import (
    InterruptSource,
    PlanExecution,
    PlanInterruptedError,
)
from goapauto.models.goal import Goal
from goapauto.models.goap_planner import (
    PlanExecutionError,
    Planner,
    PlanResult,
)
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


@pytest.fixture
def planner() -> Planner:
    return Planner(actions_list=ACTIONS, verbose=False)


@pytest.fixture
def goal() -> Goal:
    return Goal(target_state={"s": 3}, name="reach-three")


def make_execution(
    planner: Planner, plan=None, state: WorldState | None = None
) -> PlanExecution:
    return planner.begin_execution(
        state if state is not None else WorldState(s=0),
        plan if plan is not None else ["step1", "step2", "step3"],
    )


class TestPublicAPI:
    def test_new_symbols_reexported_and_in_all(self):
        for name in ("PlanExecution", "PlanInterruptedError", "InterruptSource"):
            assert name in goapauto.__all__
            assert hasattr(goapauto, name)

    def test_interrupt_source_literal_members(self):
        import typing

        assert set(typing.get_args(InterruptSource)) == {
            "manual",
            "sensor",
            "watchdog",
            "budget",
            "arbitration",
            "confidence",
            "cancellation",
        }

    def test_on_execution_interrupted_hook_is_registered(self, planner: Planner):
        assert "on_execution_interrupted" in planner.hooks
        # The existing hook events are unchanged (01 added on_budget_exhausted).
        assert set(planner.hooks) == {
            "on_node_expanded",
            "on_plan_found",
            "on_search_failed",
            "on_budget_exhausted",
            "on_action_start",
            "on_action_complete",
            "on_action_failed",
            "on_execution_complete",
            "on_execution_failed",
            "on_execution_interrupted",
        }

    def test_register_unknown_hook_still_raises(self, planner: Planner):
        with pytest.raises(ValueError, match="Unknown event hook"):
            planner.register_hook("on_execution_interrupt", lambda **kw: None)


class TestHandleLifecycle:
    def test_begin_execution_pre_run_properties(self, planner: Planner):
        state = WorldState(s=0)
        execution = planner.begin_execution(state, ["step1", "step2"])

        assert isinstance(execution, PlanExecution)
        assert execution.is_running is False
        assert execution.is_complete is False
        assert execution.is_interrupted is False
        assert execution.executed_actions == ()
        assert execution.remaining_actions == ("step1", "step2")
        assert execution.interrupt_reason is None
        assert execution.interrupt_source is None
        # Pre-run state is a deep copy of the passed initial_state.
        assert execution.state.get_state() == {"s": 0}
        assert execution.state is not state

    def test_run_uninterrupted_returns_final_state(self, planner: Planner):
        execution = make_execution(planner)
        final = execution.run()

        assert final.get_state() == {"s": 3}
        assert execution.is_complete is True
        assert execution.is_running is False
        assert execution.is_interrupted is False
        assert execution.executed_actions == ("step1", "step2", "step3")
        assert execution.remaining_actions == ()

    def test_run_accepts_plan_result(self, planner: Planner, goal: Goal):
        result = planner.generate_plan(WorldState(s=0), goal)
        assert result.plan == ["step1", "step2", "step3"]
        execution = planner.begin_execution(WorldState(s=0), result, goal)
        final = execution.run()
        assert final.get_state() == {"s": 3}

    def test_run_accepts_tuple_plan(self, planner: Planner):
        execution = planner.begin_execution(WorldState(s=0), ("step1", "step2"))
        assert execution.remaining_actions == ("step1", "step2")
        assert execution.run().get_state() == {"s": 2}

    def test_run_accepts_action_object_steps(self, planner: Planner):
        objs = [
            Action(name="step1", preconditions={"s": 0}, effects={"s": 1}, cost=1.0),
            Action(name="step2", preconditions={"s": 1}, effects={"s": 2}, cost=1.0),
        ]
        execution = planner.begin_execution(WorldState(s=0), objs)
        # Normalized to name strings at begin_execution time.
        assert execution.remaining_actions == ("step1", "step2")
        assert execution.run().get_state() == {"s": 2}
        assert execution.executed_actions == ("step1", "step2")

    def test_run_threads_goal_to_providers(self, planner: Planner):
        seen_goals = []

        class GoalAwareProvider(ActionProvider):
            def provide_actions(self, state, goal=None):
                seen_goals.append(goal)
                return []

        planner.providers.insert(0, GoalAwareProvider())
        goal = Goal(target_state={"s": 3}, name="g")
        execution = planner.begin_execution(WorldState(s=0), ["step1"], goal)
        execution.run()
        assert seen_goals and all(g is goal for g in seen_goals)

    def test_run_twice_raises(self, planner: Planner):
        execution = make_execution(planner)
        execution.run()
        with pytest.raises(RuntimeError, match="already been run"):
            execution.run()

    async def test_arun_twice_raises(self, planner: Planner):
        execution = make_execution(planner)
        await execution.arun()
        with pytest.raises(RuntimeError, match="already been run"):
            await execution.arun()

    async def test_run_then_arun_raises(self, planner: Planner):
        execution = make_execution(planner)
        execution.run()
        with pytest.raises(RuntimeError, match="already been run"):
            await execution.arun()

    def test_begin_execution_rejects_non_worldstate(self, planner: Planner):
        with pytest.raises(TypeError, match="initial_state must be a WorldState"):
            planner.begin_execution({"s": 0}, ["step1"])  # type: ignore[arg-type]

    def test_begin_execution_rejects_none_plan_result(self, planner: Planner):
        with pytest.raises(ValueError, match="no valid plan"):
            planner.begin_execution(
                WorldState(s=0), PlanResult(plan=None, message="nope")
            )

    def test_begin_execution_rejects_bad_container(self, planner: Planner):
        with pytest.raises(TypeError, match="plan must be a list"):
            planner.begin_execution(WorldState(s=0), "step1")  # type: ignore[arg-type]

    def test_begin_execution_rejects_bad_step_type_eagerly(self, planner: Planner):
        with pytest.raises(TypeError, match="Plan step must be an Action"):
            planner.begin_execution(WorldState(s=0), ["step1", 42])  # type: ignore[list-item]

    def test_initial_state_mutation_between_begin_and_run_flows_in(
        self, planner: Planner
    ):
        # TOCTOU parity with execute_plan: the deep copy happens at run() start.
        state = WorldState(s=0)
        execution = planner.begin_execution(state, ["step2", "step3"])
        state.update_state({"s": 1})
        final = execution.run()
        assert final.get_state() == {"s": 3}

    def test_is_running_true_during_run(self, planner: Planner):
        observed = []
        execution = make_execution(planner)
        planner.register_hook(
            "on_action_start", lambda **kw: observed.append(execution.is_running)
        )
        execution.run()
        assert observed == [True, True, True]
        assert execution.is_running is False


class TestInterruptValidation:
    def test_empty_reason_rejected(self, planner: Planner):
        execution = make_execution(planner)
        with pytest.raises(ValueError, match="non-empty"):
            execution.interrupt("")

    def test_none_reason_rejected(self, planner: Planner):
        execution = make_execution(planner)
        with pytest.raises(ValueError, match="non-empty"):
            execution.interrupt(None)  # type: ignore[arg-type]

    def test_whitespace_reason_rejected(self, planner: Planner):
        execution = make_execution(planner)
        with pytest.raises(ValueError, match="non-empty"):
            execution.interrupt("   ")

    def test_unknown_source_rejected(self, planner: Planner):
        execution = make_execution(planner)
        with pytest.raises(ValueError, match="source"):
            execution.interrupt("x", source="bogus")  # type: ignore[arg-type]

    def test_validation_precedes_terminal_check(self, planner: Planner):
        execution = make_execution(planner, plan=["step1"])
        execution.run()
        assert execution.is_complete
        # A completed handle is a no-op for valid calls, but misuse still
        # raises: validation runs before the terminal no-op check.
        with pytest.raises(ValueError, match="non-empty"):
            execution.interrupt("")
        with pytest.raises(ValueError, match="source"):
            execution.interrupt("x", source="bogus")  # type: ignore[arg-type]

    def test_explicit_cancellation_source_allowed(self, planner: Planner):
        execution = make_execution(planner)
        assert execution.interrupt("declared", source="cancellation") is True
        assert execution.interrupt_source == "cancellation"


class TestInterruptionSemantics:
    def test_interrupt_before_first_action(self, planner: Planner):
        fired = []
        completed = []
        started = []
        planner.register_hook("on_execution_interrupted", lambda **kw: fired.append(kw))
        planner.register_hook(
            "on_execution_complete", lambda **kw: completed.append(kw)
        )
        planner.register_hook("on_action_start", lambda **kw: started.append(kw))

        execution = make_execution(planner)
        assert execution.interrupt("enemy_sighted", source="sensor") is True

        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()

        err = exc_info.value
        assert err.reason == "enemy_sighted"
        assert err.source == "sensor"
        assert err.executed_actions == []
        assert err.remaining_actions == ["step1", "step2", "step3"]
        assert err.step_index == 0
        assert err.plan_length == 3
        assert err.state_diff == {}
        assert err.state.get_state() == {"s": 0}
        assert err.next_action == "step1"

        assert len(fired) == 1
        assert completed == []
        assert started == []  # the never-started action gets no on_action_start
        assert execution.is_interrupted is True
        assert execution.is_complete is False
        assert execution.executed_actions == ()
        assert execution.remaining_actions == ("step1", "step2", "step3")

    def test_interrupt_between_actions_via_on_action_complete(self, planner: Planner):
        execution = make_execution(planner)

        def check_staleness(*, action, state) -> None:
            if action.name == "step1":
                execution.interrupt("world moved 0 -> 1", source="sensor")

        planner.register_hook("on_action_complete", check_staleness)

        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()

        err = exc_info.value
        assert err.executed_actions == ["step1"]
        assert err.remaining_actions == ["step2", "step3"]
        assert err.step_index == 1
        assert err.plan_length == 3
        assert err.next_action == "step2"
        # Partial state is the fully-applied post-step1 state.
        assert err.state.get_state() == {"s": 1}
        assert err.state_diff == {"s": (0, 1)}
        assert execution.executed_actions == ("step1",)
        assert execution.remaining_actions == ("step2", "step3")

    def test_interrupt_from_last_action_on_action_complete(self, planner: Planner):
        completed = []
        fired = []
        planner.register_hook(
            "on_execution_complete", lambda **kw: completed.append(kw)
        )
        planner.register_hook("on_execution_interrupted", lambda **kw: fired.append(kw))

        execution = make_execution(planner)

        def on_complete(*, action, state) -> None:
            if action.name == "step3":
                assert execution.interrupt("goal already satisfied", source="sensor")

        planner.register_hook("on_action_complete", on_complete)

        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()

        err = exc_info.value
        assert err.executed_actions == ["step1", "step2", "step3"]
        assert err.remaining_actions == []
        assert err.step_index == 3
        assert err.plan_length == 3
        assert err.next_action is None
        # Post-loop check: interrupted instead of completing.
        assert completed == []
        assert len(fired) == 1
        assert execution.is_interrupted is True

    def test_empty_plan_with_pre_run_interrupt(self, planner: Planner):
        fired = []
        planner.register_hook("on_execution_interrupted", lambda **kw: fired.append(kw))
        execution = planner.begin_execution(WorldState(s=0), [])
        execution.interrupt("nothing to do", source="manual")
        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()
        err = exc_info.value
        assert err.executed_actions == []
        assert err.remaining_actions == []
        assert err.step_index == 0
        assert err.plan_length == 0
        assert len(fired) == 1

    def test_empty_plan_without_interrupt_completes(self, planner: Planner):
        completed = []
        planner.register_hook(
            "on_execution_complete", lambda **kw: completed.append(kw)
        )
        execution = planner.begin_execution(WorldState(s=0), [])
        final = execution.run()
        assert final.get_state() == {"s": 0}
        assert len(completed) == 1
        assert execution.is_complete is True

    def test_interrupt_after_completion_is_noop(self, planner: Planner):
        execution = make_execution(planner, plan=["step1"])
        execution.run()
        assert execution.is_complete is True
        assert execution.interrupt("too late") is False
        assert execution.is_interrupted is False
        assert execution.interrupt_reason is None
        assert execution.executed_actions == ("step1",)

    def test_interrupt_after_failure_is_noop(self, planner: Planner):
        execution = planner.begin_execution(WorldState(s=99), ["step1"])
        with pytest.raises(PlanExecutionError):
            execution.run()
        assert execution.is_interrupted is False
        assert execution.interrupt("too late") is False

    def test_double_interrupt_first_wins(self, planner: Planner):
        execution = make_execution(planner)
        assert execution.interrupt("first", source="sensor") is True
        assert execution.interrupt("second", source="watchdog") is False
        assert execution.interrupt_reason == "first"
        assert execution.interrupt_source == "sensor"
        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()
        assert exc_info.value.reason == "first"
        assert exc_info.value.source == "sensor"
        # Terminal: further interrupts are no-ops.
        assert execution.interrupt("third") is False

    def test_interrupt_wins_over_inapplicable_next_action(self, planner: Planner):
        # step1 is inapplicable at s=99, but the pending interrupt is honored
        # first; the precondition fallout surfaces on the replan path.
        execution = planner.begin_execution(WorldState(s=99), ["step1", "step2"])
        execution.interrupt("stop", source="manual")
        with pytest.raises(PlanInterruptedError):
            execution.run()
        with pytest.raises(PlanExecutionError):
            planner.execute_plan(WorldState(s=99), ["step1"])

    def test_unknown_action_name_still_key_error(self, planner: Planner):
        execution = planner.begin_execution(WorldState(s=0), ["nope"])
        with pytest.raises(KeyError):
            execution.run()
        assert execution.is_interrupted is False

    def test_inapplicable_action_without_interrupt_still_fails(self, planner: Planner):
        failed = []
        planner.register_hook("on_execution_failed", lambda **kw: failed.append(kw))
        execution = planner.begin_execution(WorldState(s=99), ["step1"])
        with pytest.raises(PlanExecutionError):
            execution.run()
        assert len(failed) == 1
        assert execution.is_interrupted is False
        assert execution.is_complete is False

    def test_handler_exception_fires_failure_hooks(self, planner: Planner):
        action_failed = []
        execution_failed = []

        def boom(state, action):
            raise RuntimeError("boom")

        planner.register_execution_handler("step2", boom)
        planner.register_hook("on_action_failed", lambda **kw: action_failed.append(kw))
        planner.register_hook(
            "on_execution_failed", lambda **kw: execution_failed.append(kw)
        )
        execution = make_execution(planner)
        with pytest.raises(RuntimeError, match="boom"):
            execution.run()
        assert len(action_failed) == 1
        assert len(execution_failed) == 1
        assert execution.is_complete is False
        assert execution.is_interrupted is False

    def test_flag_set_then_handler_raises_before_boundary_marks_failed(
        self, planner: Planner
    ):
        execution_failed = []
        execution = make_execution(planner)

        def bad_handler(state, action):
            execution.interrupt("abort", source="manual")
            raise RuntimeError("boom")

        planner.register_execution_handler("step1", bad_handler)
        planner.register_hook(
            "on_execution_failed", lambda **kw: execution_failed.append(kw)
        )
        with pytest.raises(RuntimeError, match="boom"):
            execution.run()
        # The run did not stop because of the interrupt: terminal is failed.
        assert execution.is_interrupted is False
        assert execution.is_complete is False
        assert len(execution_failed) == 1
        # ...but the recorded request is still inspectable for diagnostics.
        assert execution.interrupt_reason == "abort"
        assert execution.interrupt_source == "manual"

    def test_state_diff_contents(self, planner: Planner):
        execution = make_execution(planner)
        planner.register_hook(
            "on_action_complete",
            lambda *, action, state: (
                execution.interrupt("stop", source="manual")
                if action.name == "step2"
                else None
            ),
        )
        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()
        err = exc_info.value
        assert err.state_diff == {"s": (0, 2)}
        assert err.state.get_state() == {"s": 2}

    def test_err_state_is_defensive_copy(self, planner: Planner):
        execution = make_execution(planner)
        planner.register_hook(
            "on_action_complete",
            lambda *, action, state: (
                execution.interrupt("stop", source="manual")
                if action.name == "step1"
                else None
            ),
        )
        with pytest.raises(PlanInterruptedError) as exc_info:
            execution.run()
        err = exc_info.value
        live_before = execution.state.get_state()
        err.state.update_state({"s": 999, "junk": True})
        # Mutating err.state cannot corrupt the handle's live state.
        assert execution.state.get_state() == live_before == {"s": 1}
        assert err.state.get_state() == {"s": 999, "junk": True}

    def test_error_is_value_error_sibling_not_child(self):
        assert issubclass(PlanInterruptedError, ValueError)
        assert not issubclass(PlanInterruptedError, PlanExecutionError)

    def test_error_carries_all_payload_fields(self):
        state = WorldState(s=1)
        err = PlanInterruptedError(
            state=state,
            executed_actions=["step1"],
            remaining_actions=["step2"],
            step_index=1,
            plan_length=2,
            state_diff={"s": (0, 1)},
            reason="r",
            source="watchdog",
        )
        assert err.state is state
        assert err.executed_actions == ["step1"]
        assert err.remaining_actions == ["step2"]
        assert err.step_index == 1
        assert err.plan_length == 2
        assert err.state_diff == {"s": (0, 1)}
        assert err.reason == "r"
        assert err.source == "watchdog"
        assert err.next_action == "step2"
        assert str(err)  # deterministic default message

    def test_error_next_action_none_when_nothing_remains(self):
        err = PlanInterruptedError(
            state=WorldState(s=3),
            executed_actions=["step1"],
            remaining_actions=[],
            step_index=1,
            plan_length=1,
            state_diff={},
            reason="r",
            source="manual",
        )
        assert err.next_action is None

    def test_error_custom_message_preserved(self):
        err = PlanInterruptedError(
            "custom!",
            state=WorldState(),
            executed_actions=[],
            remaining_actions=[],
            step_index=0,
            plan_length=0,
            state_diff={},
            reason="r",
            source="manual",
        )
        assert str(err) == "custom!"

    def test_error_rejects_empty_reason(self):
        with pytest.raises(ValueError, match="non-empty"):
            PlanInterruptedError(
                state=WorldState(),
                executed_actions=[],
                remaining_actions=[],
                step_index=0,
                plan_length=0,
                state_diff={},
                reason="  ",
                source="manual",
            )

    def test_error_rejects_unknown_source(self):
        with pytest.raises(ValueError, match="source"):
            PlanInterruptedError(
                state=WorldState(),
                executed_actions=[],
                remaining_actions=[],
                step_index=0,
                plan_length=0,
                state_diff={},
                reason="r",
                source="bogus",  # type: ignore[arg-type]
            )

    def test_catch_guidance_sibling_ordering(self, planner: Planner):
        # A shared wrapper catching PlanExecutionError must NOT swallow a
        # deliberate interruption (failure-metrics poisoning guard).
        execution = make_execution(planner, plan=["step1"])
        execution.interrupt("stop", source="manual")
        with pytest.raises(PlanInterruptedError):
            try:
                execution.run()
            except PlanExecutionError:
                pytest.fail("interruption must not be caught as a failure")
