"""Tests for ReplanPolicy (design 01): replan throttling and circuit breaking."""

import json

import pytest

from goapauto import (
    Planner,
    ReplanDecision,
    ReplanPolicy,
    ReplanReason,
)
from goapauto.models.goal import Goal
from goapauto.models.worldstate import WorldState


class FakeClock:
    """Injectable monotonic clock for deterministic timing tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_planner() -> Planner:
    return Planner(
        actions_list=[
            ("pickup_key", {}, {"has_key": True}, 1.0),
            ("open_door", {"has_key": True}, {"door_open": True}, 1.0),
        ],
        verbose=False,
    )


def make_state(**overrides) -> WorldState:
    base = {"has_key": False, "door_open": False, "enemy_visible": False, "health": 100}
    base.update(overrides)
    return WorldState(**base)


def make_goal() -> Goal:
    return Goal(target_state={"door_open": True})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def planner() -> Planner:
    return make_planner()


@pytest.fixture
def state() -> WorldState:
    return make_state()


@pytest.fixture
def goal() -> Goal:
    return make_goal()


@pytest.fixture
def policy(planner: Planner, clock: FakeClock) -> ReplanPolicy:
    return ReplanPolicy(planner, clock=clock)


@pytest.fixture
def adopted(policy: ReplanPolicy, state: WorldState, goal: Goal) -> ReplanPolicy:
    policy.adopt(state, goal)
    return policy


class TestConstruction:
    def test_defaults_are_opt_in(self, planner: Planner, clock: FakeClock):
        policy = ReplanPolicy(planner, clock=clock)
        assert policy._min_replan_interval == 0.0
        assert policy._replan_on_invalid is True
        assert policy._replan_on_goal_change is True
        assert policy._watched_keys is None
        assert policy._max_sensor_age is None
        assert policy._time_budget is None
        assert policy._max_replans_per_tick is None
        assert policy._max_consecutive_unthrottled_replans is None
        assert policy._reuse_prefix_on_invalid is True

    @pytest.mark.parametrize("bad", [-1.0, -0.5, float("nan")])
    def test_bad_min_replan_interval_value_error(
        self, planner: Planner, clock: FakeClock, bad: float
    ):
        with pytest.raises(ValueError):
            ReplanPolicy(planner, min_replan_interval=bad, clock=clock)

    @pytest.mark.parametrize("bad", [True, False, "1.0", None])
    def test_bad_min_replan_interval_type_error(
        self, planner: Planner, clock: FakeClock, bad
    ):
        with pytest.raises(TypeError):
            ReplanPolicy(planner, min_replan_interval=bad, clock=clock)

    def test_negative_max_sensor_age_value_error(
        self, planner: Planner, clock: FakeClock
    ):
        with pytest.raises(ValueError):
            ReplanPolicy(planner, max_sensor_age=-1.0, clock=clock)

    def test_bool_max_sensor_age_type_error(self, planner: Planner, clock: FakeClock):
        with pytest.raises(TypeError):
            ReplanPolicy(planner, max_sensor_age=True, clock=clock)

    @pytest.mark.parametrize("bad", [0, -1.0, float("nan")])
    def test_bad_policy_time_budget_value_error(
        self, planner: Planner, clock: FakeClock, bad: float
    ):
        with pytest.raises(ValueError):
            ReplanPolicy(planner, time_budget=bad, clock=clock)

    @pytest.mark.parametrize("bad", ["fast", True])
    def test_bad_policy_time_budget_type_error(
        self, planner: Planner, clock: FakeClock, bad
    ):
        with pytest.raises(TypeError):
            ReplanPolicy(planner, time_budget=bad, clock=clock)

    @pytest.mark.parametrize("bad", [0, -3])
    def test_bad_max_replans_per_tick_value_error(
        self, planner: Planner, clock: FakeClock, bad: int
    ):
        with pytest.raises(ValueError):
            ReplanPolicy(planner, max_replans_per_tick=bad, clock=clock)

    @pytest.mark.parametrize("bad", [True, 1.5, "2"])
    def test_bad_max_replans_per_tick_type_error(
        self, planner: Planner, clock: FakeClock, bad
    ):
        with pytest.raises(TypeError):
            ReplanPolicy(planner, max_replans_per_tick=bad, clock=clock)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_bad_circuit_threshold_value_error(
        self, planner: Planner, clock: FakeClock, bad: int
    ):
        with pytest.raises(ValueError):
            ReplanPolicy(planner, max_consecutive_unthrottled_replans=bad, clock=clock)

    def test_bool_circuit_threshold_type_error(
        self, planner: Planner, clock: FakeClock
    ):
        with pytest.raises(TypeError):
            ReplanPolicy(planner, max_consecutive_unthrottled_replans=True, clock=clock)

    @pytest.mark.parametrize("param", ["replan_on_invalid", "replan_on_goal_change"])
    def test_non_bool_flags_type_error(
        self, planner: Planner, clock: FakeClock, param: str
    ):
        with pytest.raises(TypeError):
            ReplanPolicy(planner, **{param: 1}, clock=clock)

    def test_non_bool_reuse_prefix_type_error(self, planner: Planner, clock: FakeClock):
        with pytest.raises(TypeError):
            ReplanPolicy(planner, reuse_prefix_on_invalid="yes", clock=clock)

    def test_unknown_hook_event_raises(self, policy: ReplanPolicy):
        with pytest.raises(ValueError):
            policy.register_hook("on_replan_done", lambda decision: None)


class TestAdoption:
    def test_should_replan_before_adopt_raises(
        self, policy: ReplanPolicy, state: WorldState, goal: Goal
    ):
        with pytest.raises(ValueError, match="adopt"):
            policy.should_replan(state, goal, ["pickup_key"])

    def test_adopt_then_no_phantom_goal_changed(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        decision = adopted.should_replan(state, goal, ["pickup_key", "open_door"])
        assert decision.replan is False
        assert decision.reason == ReplanReason.PLAN_VALID

    def test_adopt_rejects_dicts(self, policy: ReplanPolicy, goal: Goal):
        with pytest.raises(TypeError):
            policy.adopt({"has_key": False}, goal)
        with pytest.raises(TypeError):
            policy.adopt(make_state(), {"door_open": True})

    def test_reset_requires_adopt_again(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        adopted.reset()
        with pytest.raises(ValueError, match="adopt"):
            adopted.should_replan(state, goal, ["pickup_key"])

    def test_should_replan_rejects_bad_types(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        with pytest.raises(TypeError):
            adopted.should_replan({"has_key": False}, goal, ["pickup_key"])
        with pytest.raises(TypeError):
            adopted.should_replan(state, {"door_open": True}, ["pickup_key"])
        with pytest.raises(TypeError):
            adopted.should_replan(state, goal, 42)
        with pytest.raises(ValueError):
            adopted.should_replan(state, goal, ["pickup_key"], next_index=-1)


class TestNoPlan:
    def test_none_plan_triggers_even_with_huge_interval(
        self, state: WorldState, goal: Goal, planner: Planner, clock: FakeClock
    ):
        policy = ReplanPolicy(planner, min_replan_interval=3600.0, clock=clock)
        policy.adopt(state, goal)
        decision = policy.should_replan(state, goal, None)
        assert decision.replan is True
        assert decision.reason == ReplanReason.NO_PLAN
        assert decision.detail == {}

    def test_plan_result_unwrapped(self, adopted: ReplanPolicy, state, goal, planner):
        result = planner.generate_plan(state, goal)
        decision = adopted.should_replan(state, goal, result)
        assert decision.reason == ReplanReason.PLAN_VALID

    def test_plan_result_none_unwrapped_to_no_plan(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal, planner: Planner
    ):
        planner.max_iterations = 1
        result = planner.generate_plan(state, goal)
        assert result.plan is None
        decision = adopted.should_replan(state, goal, result)
        assert decision.reason == ReplanReason.NO_PLAN
        assert decision.replan is True

    def test_next_index_past_end_is_no_plan(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        decision = adopted.should_replan(
            state, goal, ["pickup_key", "open_door"], next_index=2
        )
        assert decision.reason == ReplanReason.NO_PLAN
        assert decision.replan is True

    def test_empty_plan_is_no_plan_when_goal_unsatisfied(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        decision = adopted.should_replan(state, goal, [])
        assert decision.reason == ReplanReason.NO_PLAN


class TestGoalSatisfied:
    def test_satisfied_goal_no_replan_no_planning(
        self, adopted: ReplanPolicy, goal: Goal, monkeypatch
    ):
        satisfied = make_state(door_open=True)
        calls = []
        monkeypatch.setattr(
            adopted._planner, "generate_plan", lambda *a, **k: calls.append(1)
        )
        decision = adopted.should_replan(satisfied, goal, ["pickup_key"])
        assert decision.replan is False
        assert decision.reason == ReplanReason.GOAL_SATISFIED
        assert calls == []


class TestPlanInvalid:
    def test_inapplicable_next_action_triggers(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        # state has no key: open_door is not applicable
        decision = adopted.should_replan(state, goal, ["open_door"])
        assert decision.replan is True
        assert decision.reason == ReplanReason.PLAN_INVALID
        assert decision.detail == {"action": "open_door", "interrupted": False}

    def test_unknown_action_name_triggers(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        decision = adopted.should_replan(state, goal, ["nope"])
        assert decision.reason == ReplanReason.PLAN_INVALID
        assert decision.detail["action"] == "nope"

    def test_action_objects_in_plan_supported(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal, planner: Planner
    ):
        action = planner.get_action("open_door", state)
        assert action is not None
        decision = adopted.should_replan(state, goal, [action])
        assert decision.reason == ReplanReason.PLAN_INVALID
        assert decision.detail["action"] == "open_door"

    def test_non_string_plan_step_raises(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        with pytest.raises(TypeError):
            adopted.should_replan(state, goal, [42])

    def test_bypasses_min_replan_interval(
        self,
        state: WorldState,
        goal: Goal,
        planner: Planner,
        clock: FakeClock,
    ):
        policy = ReplanPolicy(planner, min_replan_interval=3600.0, clock=clock)
        policy.adopt(state, goal)
        decision = policy.should_replan(state, goal, ["open_door"])
        assert decision.replan is True
        assert decision.reason == ReplanReason.PLAN_INVALID

    def test_replan_on_invalid_false_skips_check(
        self,
        state: WorldState,
        goal: Goal,
        planner: Planner,
        clock: FakeClock,
    ):
        policy = ReplanPolicy(planner, replan_on_invalid=False, clock=clock)
        policy.adopt(state, goal)
        decision = policy.should_replan(state, goal, ["open_door"])
        assert decision.reason == ReplanReason.PLAN_VALID

    def test_was_interrupted_forces_invalid_on_applicable_action(
        self, adopted: ReplanPolicy, goal: Goal
    ):
        keyed = make_state(has_key=True)
        decision = adopted.should_replan(
            keyed, goal, ["open_door"], was_interrupted=True
        )
        assert decision.replan is True
        assert decision.reason == ReplanReason.PLAN_INVALID
        assert decision.detail == {"action": "open_door", "interrupted": True}

    def test_replan_routes_through_continue_plan(
        self,
        adopted: ReplanPolicy,
        state: WorldState,
        goal: Goal,
        planner: Planner,
        monkeypatch,
    ):
        calls = []

        def spy(world_state, goal_, executed_actions=None, time_budget=None):
            calls.append(
                {"executed_actions": list(executed_actions), "time_budget": time_budget}
            )
            return planner.__class__.continue_plan(
                planner, world_state, goal_, executed_actions or []
            )

        monkeypatch.setattr(planner, "continue_plan", spy)
        decision = adopted.should_replan(state, goal, ["open_door"])
        adopted.note_executed("pickup_key")
        result = adopted.replan(state, goal, decision)
        assert len(calls) == 1
        assert calls[0]["executed_actions"] == ["pickup_key"]
        assert calls[0]["time_budget"] is None
        assert result.plan is not None

    def test_policy_time_budget_passed_through(
        self,
        state: WorldState,
        goal: Goal,
        planner: Planner,
        clock: FakeClock,
        monkeypatch,
    ):
        policy = ReplanPolicy(planner, time_budget=0.05, clock=clock)
        policy.adopt(state, goal)
        seen = {}

        def spy(world_state, goal_, executed_actions=None, time_budget=None):
            seen["time_budget"] = time_budget
            return planner.__class__.continue_plan(
                planner, world_state, goal_, executed_actions or []
            )

        monkeypatch.setattr(planner, "continue_plan", spy)
        decision = policy.should_replan(state, goal, ["open_door"])
        policy.replan(state, goal, decision)
        assert seen["time_budget"] == 0.05


class TestGoalChanged:
    def test_goal_change_triggers_fresh_plan(
        self,
        adopted: ReplanPolicy,
        state: WorldState,
        goal: Goal,
        planner: Planner,
        monkeypatch,
    ):
        new_goal = Goal(target_state={"has_key": True})
        decision = adopted.should_replan(state, new_goal, ["pickup_key"])
        assert decision.replan is True
        assert decision.reason == ReplanReason.GOAL_CHANGED

        calls = []
        orig = planner.__class__.generate_plan

        def spy(self_, world_state, goal_, **kwargs):
            calls.append(kwargs.get("time_budget", "unset"))
            return orig(self_, world_state, goal_, **kwargs)

        monkeypatch.setattr(planner, "generate_plan", spy.__get__(planner))
        result = adopted.replan(state, new_goal, decision)
        assert len(calls) == 1
        assert result.plan == ["pickup_key"]

    def test_replan_on_goal_change_false_skips(
        self,
        state: WorldState,
        goal: Goal,
        planner: Planner,
        clock: FakeClock,
    ):
        policy = ReplanPolicy(planner, replan_on_goal_change=False, clock=clock)
        policy.adopt(state, goal)
        new_goal = Goal(target_state={"has_key": True})
        decision = policy.should_replan(state, new_goal, ["pickup_key", "open_door"])
        assert decision.reason == ReplanReason.PLAN_VALID


class TestWorldChanged:
    @pytest.fixture
    def watched(self, planner: Planner, clock: FakeClock) -> ReplanPolicy:
        return ReplanPolicy(
            planner, watched_keys=frozenset({"enemy_visible"}), clock=clock
        )

    def test_watched_key_flip_triggers(
        self, watched: ReplanPolicy, state: WorldState, goal: Goal
    ):
        watched.adopt(state, goal)
        moved = make_state(enemy_visible=True)
        decision = watched.should_replan(moved, goal, ["pickup_key", "open_door"])
        assert decision.replan is True
        assert decision.reason == ReplanReason.WORLD_CHANGED
        assert decision.detail == {"changed_keys": ["enemy_visible"]}

    def test_unwatched_key_flip_is_plan_valid(
        self, watched: ReplanPolicy, state: WorldState, goal: Goal
    ):
        watched.adopt(state, goal)
        moved = make_state(health=50)
        decision = watched.should_replan(moved, goal, ["pickup_key", "open_door"])
        assert decision.reason == ReplanReason.PLAN_VALID

    def test_watched_keys_none_never_fires(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        moved = make_state(enemy_visible=True)
        decision = adopted.should_replan(moved, goal, ["pickup_key", "open_door"])
        assert decision.reason == ReplanReason.PLAN_VALID

    def test_world_changed_replan_uses_continue_plan(
        self,
        watched: ReplanPolicy,
        state: WorldState,
        goal: Goal,
        planner: Planner,
        monkeypatch,
    ):
        watched.adopt(state, goal)
        moved = make_state(enemy_visible=True)
        decision = watched.should_replan(moved, goal, ["pickup_key", "open_door"])

        calls = []
        orig = planner.__class__.continue_plan

        def spy(self_, world_state, goal_, executed_actions=None, **kwargs):
            calls.append(True)
            return orig(self_, world_state, goal_, executed_actions or [], **kwargs)

        monkeypatch.setattr(planner, "continue_plan", spy.__get__(planner))
        result = watched.replan(moved, goal, decision)
        assert calls == [True]
        assert result.plan == ["pickup_key", "open_door"]


class TestThrottled:
    @pytest.fixture
    def throttled_policy(self, planner: Planner, clock: FakeClock) -> ReplanPolicy:
        return ReplanPolicy(
            planner,
            watched_keys=frozenset({"enemy_visible"}),
            min_replan_interval=10.0,
            clock=clock,
        )

    def test_world_change_within_interval_throttled(
        self,
        throttled_policy: ReplanPolicy,
        state: WorldState,
        goal: Goal,
        clock: FakeClock,
    ):
        throttled_policy.adopt(state, goal)
        clock.advance(1.0)
        moved = make_state(enemy_visible=True)
        decision = throttled_policy.should_replan(
            moved, goal, ["pickup_key", "open_door"]
        )
        assert decision.replan is False
        assert decision.reason == ReplanReason.THROTTLED
        assert decision.detail["throttled_by"] == "min_replan_interval"
        assert decision.detail["changed_keys"] == ["enemy_visible"]
        assert decision.detail["elapsed"] == pytest.approx(1.0)
        assert decision.detail["cooldown_remaining_ms"] == pytest.approx(9000.0)

    def test_replan_restarts_throttle_clock(
        self, planner: Planner, state: WorldState, goal: Goal
    ):
        """The implicit adoption inside replan() restarts min_replan_interval."""
        clock = FakeClock(start=0.0)
        policy = ReplanPolicy(
            planner,
            min_replan_interval=10.0,
            clock=clock,
            watched_keys=frozenset({"enemy_visible"}),
        )
        policy.adopt(state, goal)  # t=0
        decision = policy.should_replan(state, goal, None)
        assert decision.reason == ReplanReason.NO_PLAN
        clock.now = 5.0
        policy.replan(state, goal, decision)  # implicit adoption at t=5
        clock.now = 12.0  # 7s after the replan, 12s after the adopt
        moved = make_state(enemy_visible=True)
        second = policy.should_replan(moved, goal, ["pickup_key"])
        assert second.reason == ReplanReason.THROTTLED
        assert second.detail["cooldown_remaining_ms"] == pytest.approx(3000.0)

    def test_replan_after_interval_elapses(
        self,
        throttled_policy: ReplanPolicy,
        state: WorldState,
        goal: Goal,
        clock: FakeClock,
    ):
        throttled_policy.adopt(state, goal)
        clock.advance(11.0)
        moved = make_state(enemy_visible=True)
        decision = throttled_policy.should_replan(
            moved, goal, ["pickup_key", "open_door"]
        )
        assert decision.replan is True
        assert decision.reason == ReplanReason.WORLD_CHANGED

    def test_on_replan_skipped_fires(
        self,
        throttled_policy: ReplanPolicy,
        state: WorldState,
        goal: Goal,
        clock: FakeClock,
    ):
        throttled_policy.adopt(state, goal)
        clock.advance(1.0)
        calls = []
        throttled_policy.register_hook(
            "on_replan_skipped", lambda decision: calls.append(decision)
        )
        moved = make_state(enemy_visible=True)
        decision = throttled_policy.should_replan(
            moved, goal, ["pickup_key", "open_door"]
        )
        assert len(calls) == 1
        assert calls[0] is decision

    def test_plan_valid_never_fires_skip_hook(
        self, throttled_policy: ReplanPolicy, state: WorldState, goal: Goal
    ):
        throttled_policy.adopt(state, goal)
        calls = []
        throttled_policy.register_hook(
            "on_replan_skipped", lambda decision: calls.append(decision)
        )
        decision = throttled_policy.should_replan(state, goal, ["pickup_key"])
        assert decision.reason == ReplanReason.PLAN_VALID
        assert calls == []

    def test_emit_hooks_false_is_side_effect_free(
        self,
        throttled_policy: ReplanPolicy,
        state: WorldState,
        goal: Goal,
        clock: FakeClock,
    ):
        throttled_policy.adopt(state, goal)
        clock.advance(1.0)
        calls = []
        throttled_policy.register_hook(
            "on_replan_skipped", lambda decision: calls.append(decision)
        )
        moved = make_state(enemy_visible=True)
        decision = throttled_policy.should_replan(
            moved, goal, ["pickup_key", "open_door"], emit_hooks=False
        )
        assert decision.reason == ReplanReason.THROTTLED
        assert calls == []


class TestSensorsStale:
    @pytest.fixture
    def stale_policy(self, planner: Planner, clock: FakeClock) -> ReplanPolicy:
        return ReplanPolicy(
            planner,
            watched_keys=frozenset({"enemy_visible"}),
            max_sensor_age=5.0,
            clock=clock,
        )

    def test_stale_suppresses_world_change_replan(
        self, stale_policy: ReplanPolicy, state: WorldState, goal: Goal
    ):
        stale_policy.adopt(state, goal)
        moved = make_state(enemy_visible=True)
        decision = stale_policy.should_replan(
            moved, goal, ["pickup_key", "open_door"], sensor_age=10.0
        )
        assert decision.replan is False
        assert decision.reason == ReplanReason.SENSORS_STALE
        assert decision.detail["sensor_age"] == 10.0

    def test_stale_does_not_suppress_plan_invalid(
        self, stale_policy: ReplanPolicy, state: WorldState, goal: Goal
    ):
        stale_policy.adopt(state, goal)
        decision = stale_policy.should_replan(
            state, goal, ["open_door"], sensor_age=10.0
        )
        assert decision.replan is True
        assert decision.reason == ReplanReason.PLAN_INVALID

    def test_stale_beats_throttled(
        self, planner: Planner, clock: FakeClock, state: WorldState, goal: Goal
    ):
        policy = ReplanPolicy(
            planner,
            watched_keys=frozenset({"enemy_visible"}),
            min_replan_interval=3600.0,
            max_sensor_age=5.0,
            clock=clock,
        )
        policy.adopt(state, goal)
        moved = make_state(enemy_visible=True)
        decision = policy.should_replan(
            moved, goal, ["pickup_key", "open_door"], sensor_age=10.0
        )
        assert decision.reason == ReplanReason.SENSORS_STALE

    def test_stale_skip_hook_fires(
        self, stale_policy: ReplanPolicy, state: WorldState, goal: Goal
    ):
        stale_policy.adopt(state, goal)
        calls = []
        stale_policy.register_hook(
            "on_replan_skipped", lambda decision: calls.append(decision)
        )
        moved = make_state(enemy_visible=True)
        stale_policy.should_replan(
            moved, goal, ["pickup_key", "open_door"], sensor_age=10.0
        )
        assert len(calls) == 1


class TestCircuitBreaker:
    @pytest.fixture
    def breaking(self, planner: Planner, clock: FakeClock) -> ReplanPolicy:
        return ReplanPolicy(planner, max_consecutive_unthrottled_replans=2, clock=clock)

    def test_circuit_opens_after_threshold(
        self, breaking: ReplanPolicy, state: WorldState, goal: Goal
    ):
        breaking.adopt(state, goal)
        for _ in range(2):
            decision = breaking.should_replan(state, goal, ["open_door"])
            assert decision.reason == ReplanReason.PLAN_INVALID
            breaking.replan(state, goal, decision)
        third = breaking.should_replan(state, goal, ["open_door"])
        assert third.replan is False
        assert third.reason == ReplanReason.THROTTLED
        assert third.detail["throttled_by"] == "circuit_breaker"
        assert third.detail["consecutive_unthrottled_replans"] == 2
        assert third.detail["last_replan_tick_id"] is None

    def test_plan_valid_closes_circuit(
        self, breaking: ReplanPolicy, state: WorldState, goal: Goal
    ):
        breaking.adopt(state, goal)
        for _ in range(2):
            decision = breaking.should_replan(state, goal, ["open_door"])
            breaking.replan(state, goal, decision)
        assert breaking.should_replan(state, goal, ["open_door"]).reason == (
            ReplanReason.THROTTLED
        )
        # a usable plan again: steady state resets the breaker
        keyed = make_state(has_key=True)
        valid = breaking.should_replan(keyed, goal, ["open_door"])
        assert valid.reason == ReplanReason.PLAN_VALID
        again = breaking.should_replan(state, goal, ["open_door"])
        assert again.replan is True
        assert again.reason == ReplanReason.PLAN_INVALID

    def test_world_changed_probe_allowed_while_open(
        self, planner: Planner, clock: FakeClock, state: WorldState, goal: Goal
    ):
        policy = ReplanPolicy(
            planner,
            watched_keys=frozenset({"enemy_visible"}),
            max_consecutive_unthrottled_replans=1,
            clock=clock,
        )
        policy.adopt(state, goal)
        decision = policy.should_replan(state, goal, ["open_door"])
        policy.replan(state, goal, decision)
        assert policy.should_replan(state, goal, ["open_door"]).reason == (
            ReplanReason.THROTTLED
        )
        moved = make_state(enemy_visible=True)
        probe = policy.should_replan(moved, goal, ["pickup_key", "open_door"])
        assert probe.replan is True
        assert probe.reason == ReplanReason.WORLD_CHANGED
        # the successful probe replan closes the circuit
        policy.replan(moved, goal, probe)
        assert policy._circuit_open is False
        assert policy._unthrottled_streak == 0

    def test_failed_world_changed_probe_keeps_circuit_open(
        self, planner: Planner, clock: FakeClock, state: WorldState
    ):
        """A probe that finds no plan does not close the circuit."""
        policy = ReplanPolicy(
            planner,
            watched_keys=frozenset({"enemy_visible"}),
            max_consecutive_unthrottled_replans=1,
            clock=clock,
        )
        impossible = Goal(target_state={"impossible_thing": True})
        policy.adopt(state, impossible)
        decision = policy.should_replan(state, impossible, ["open_door"])
        assert decision.reason == ReplanReason.PLAN_INVALID
        policy.replan(state, impossible, decision)
        assert policy._circuit_open is True

        moved = make_state(enemy_visible=True)
        probe = policy.should_replan(moved, impossible, ["pickup_key"])
        assert probe.replan is True
        assert probe.reason == ReplanReason.WORLD_CHANGED
        policy.replan(moved, impossible, probe)
        # no usable plan came back: the livelock streak is unbroken
        assert policy._circuit_open is True
        assert policy._unthrottled_streak == 1
        again = policy.should_replan(moved, impossible, ["open_door"])
        assert again.replan is False
        assert again.reason == ReplanReason.THROTTLED
        assert again.detail["throttled_by"] == "circuit_breaker"

    def test_adopt_closes_circuit(
        self, breaking: ReplanPolicy, state: WorldState, goal: Goal
    ):
        breaking.adopt(state, goal)
        for _ in range(2):
            breaking.replan(
                state, goal, breaking.should_replan(state, goal, ["open_door"])
            )
        assert breaking._circuit_open is True
        breaking.adopt(state, goal)
        assert breaking._circuit_open is False
        assert breaking._unthrottled_streak == 0


class TestMaxReplansPerTick:
    @pytest.fixture
    def capped(self, planner: Planner, clock: FakeClock) -> ReplanPolicy:
        return ReplanPolicy(planner, max_replans_per_tick=1, clock=clock)

    def test_second_replan_same_tick_throttled(
        self, capped: ReplanPolicy, state: WorldState, goal: Goal
    ):
        capped.adopt(state, goal)
        first = capped.should_replan(state, goal, None, tick_id=7)
        assert first.reason == ReplanReason.NO_PLAN
        capped.replan(state, goal, first, tick_id=7)
        second = capped.should_replan(state, goal, None, tick_id=7)
        assert second.replan is False
        assert second.reason == ReplanReason.THROTTLED
        assert second.detail["throttled_by"] == "max_replans_per_tick"
        assert second.detail["replans_this_tick"] == 1
        assert second.detail["max_replans_per_tick"] == 1

    def test_new_tick_resets_counter(
        self, capped: ReplanPolicy, state: WorldState, goal: Goal
    ):
        capped.adopt(state, goal)
        capped.replan(state, goal, capped.should_replan(state, goal, None, tick_id=7))
        assert capped.should_replan(state, goal, None, tick_id=7).reason == (
            ReplanReason.THROTTLED
        )
        fresh = capped.should_replan(state, goal, None, tick_id=8)
        assert fresh.replan is True
        assert fresh.reason == ReplanReason.NO_PLAN

    def test_cap_without_tick_id_raises(
        self, capped: ReplanPolicy, state: WorldState, goal: Goal
    ):
        capped.adopt(state, goal)
        with pytest.raises(ValueError, match="tick_id"):
            capped.should_replan(state, goal, None)

    def test_replan_tick_rollover_on_boundary(
        self, capped: ReplanPolicy, state: WorldState, goal: Goal
    ):
        """replan() with a different tick_id rolls the per-tick counter."""
        capped.adopt(state, goal)
        decision = capped.should_replan(state, goal, None, tick_id=7)
        capped.replan(state, goal, decision, tick_id=8)
        assert capped._last_tick_id == 8
        assert capped._last_replan_tick_id == 8
        assert capped._replans_this_tick == 1

    def test_tick_id_recorded_in_detail(
        self, capped: ReplanPolicy, state: WorldState, goal: Goal
    ):
        capped.adopt(state, goal)
        decision = capped.should_replan(state, goal, None, tick_id="tick-9")
        assert decision.detail["tick_id"] == "tick-9"


class TestReplanGuards:
    def test_replan_on_negative_decision_raises(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        decision = adopted.should_replan(state, goal, ["pickup_key"])
        assert decision.replan is False
        with pytest.raises(ValueError):
            adopted.replan(state, goal, decision)

    def test_replan_with_mismatched_objects_raises(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        decision = adopted.should_replan(state, goal, None)
        assert decision.replan is True
        with pytest.raises(ValueError):
            adopted.replan(make_state(), goal, decision)
        with pytest.raises(ValueError):
            adopted.replan(state, make_goal(), decision)

    def test_replan_with_unexpected_reason_raises(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        weird = ReplanDecision(True, ReplanReason.PLAN_VALID, {})
        with pytest.raises(ValueError):
            adopted.replan(state, goal, weird)

    def test_on_replan_hook_fires_with_result(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        calls = []
        adopted.register_hook(
            "on_replan", lambda decision, result: calls.append((decision, result))
        )
        decision = adopted.should_replan(state, goal, None)
        result = adopted.replan(state, goal, decision)
        assert len(calls) == 1
        hooked_decision, hooked_result = calls[0]
        assert hooked_decision is decision
        assert hooked_result is result
        assert hooked_result.plan == ["pickup_key", "open_door"]

    def test_replan_adopts_implicitly(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        decision = adopted.should_replan(state, goal, None)
        adopted.replan(state, goal, decision)
        # snapshot/goal refreshed; a re-decide against the same objects is clean
        followup = adopted.should_replan(state, goal, ["pickup_key", "open_door"])
        assert followup.reason == ReplanReason.PLAN_VALID


class TestReusePrefixFlag:
    def test_reuse_prefix_false_routes_to_generate_plan(
        self,
        state: WorldState,
        goal: Goal,
        planner: Planner,
        clock: FakeClock,
        monkeypatch,
    ):
        policy = ReplanPolicy(planner, reuse_prefix_on_invalid=False, clock=clock)
        policy.adopt(state, goal)
        decision = policy.should_replan(state, goal, ["open_door"])
        assert decision.reason == ReplanReason.PLAN_INVALID

        calls = {"generate": 0, "continue": 0}
        orig_gen = planner.__class__.generate_plan
        orig_cont = planner.__class__.continue_plan

        def gen_spy(self_, world_state, goal_, **kwargs):
            calls["generate"] += 1
            return orig_gen(self_, world_state, goal_, **kwargs)

        def cont_spy(self_, world_state, goal_, executed_actions=None, **kwargs):
            calls["continue"] += 1
            return orig_cont(
                self_, world_state, goal_, executed_actions or [], **kwargs
            )

        monkeypatch.setattr(planner, "generate_plan", gen_spy.__get__(planner))
        monkeypatch.setattr(planner, "continue_plan", cont_spy.__get__(planner))
        policy.replan(state, goal, decision)
        assert calls == {"generate": 1, "continue": 0}


class TestNoteExecuted:
    def test_accumulates_and_clears_on_replan(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        adopted.note_executed("pickup_key")
        adopted.note_executed("open_door")
        assert adopted._executed == ["pickup_key", "open_door"]
        decision = adopted.should_replan(state, goal, None)
        adopted.replan(state, goal, decision)
        assert adopted._executed == []

    def test_non_string_raises(self, adopted: ReplanPolicy):
        with pytest.raises(TypeError):
            adopted.note_executed(42)


class TestDetailContract:
    def test_detail_json_safe_for_every_reason(
        self, planner: Planner, clock: FakeClock, state: WorldState, goal: Goal
    ):
        policy = ReplanPolicy(
            planner,
            watched_keys=frozenset({"enemy_visible"}),
            min_replan_interval=10.0,
            max_sensor_age=5.0,
            clock=clock,
        )
        policy.adopt(state, goal)
        moved = make_state(enemy_visible=True)
        satisfied = make_state(door_open=True)

        decisions = [
            policy.should_replan(state, goal, None, tick_id=1),  # NO_PLAN
            policy.should_replan(satisfied, goal, ["pickup_key"]),  # GOAL_SATISFIED
            policy.should_replan(state, goal, ["pickup_key"]),  # PLAN_VALID
            policy.should_replan(state, goal, ["open_door"]),  # PLAN_INVALID
            policy.should_replan(
                state, Goal(target_state={"has_key": True}), ["pickup_key"]
            ),  # GOAL_CHANGED
            policy.should_replan(
                moved, goal, ["pickup_key"], sensor_age=99.0
            ),  # SENSORS_STALE
        ]
        clock.advance(11.0)
        decisions.append(
            policy.should_replan(moved, goal, ["pickup_key"], tick_id=2)
            # ^ WORLD_CHANGED (interval elapsed)
        )
        policy.adopt(state, goal)  # re-adopt resets the interval clock
        decisions.append(
            policy.should_replan(moved, goal, ["pickup_key"])  # THROTTLED
        )
        reasons = {d.reason for d in decisions}
        assert reasons == {
            ReplanReason.NO_PLAN,
            ReplanReason.GOAL_SATISFIED,
            ReplanReason.PLAN_VALID,
            ReplanReason.PLAN_INVALID,
            ReplanReason.GOAL_CHANGED,
            ReplanReason.WORLD_CHANGED,
            ReplanReason.SENSORS_STALE,
            ReplanReason.THROTTLED,
        }
        for decision in decisions:
            json.dumps(decision.detail)

    def test_wire_values(self):
        assert ReplanReason.NO_PLAN.value == "no_plan"
        assert ReplanReason.GOAL_SATISFIED.value == "goal_satisfied"
        assert ReplanReason.PLAN_VALID.value == "plan_valid"
        assert ReplanReason.PLAN_INVALID.value == "plan_invalid"
        assert ReplanReason.GOAL_CHANGED.value == "goal_changed"
        assert ReplanReason.WORLD_CHANGED.value == "world_changed"
        assert ReplanReason.THROTTLED.value == "throttled"
        assert ReplanReason.SENSORS_STALE.value == "sensors_stale"

    def test_decision_equality_but_not_hashable(
        self, adopted: ReplanPolicy, state: WorldState, goal: Goal
    ):
        d1 = adopted.should_replan(state, goal, None)
        d2 = adopted.should_replan(state, goal, None)
        assert d1 == d2
        with pytest.raises(TypeError):
            hash(d1)


class TestExports:
    def test_public_symbols_reexported(self):
        import goapauto

        assert goapauto.ReplanPolicy is ReplanPolicy
        assert goapauto.ReplanDecision is ReplanDecision
        assert goapauto.ReplanReason is ReplanReason
        assert "ReplanPolicy" in goapauto.__all__
        assert "ReplanDecision" in goapauto.__all__
        assert "ReplanReason" in goapauto.__all__
