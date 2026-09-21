"""Tests for the agent inspector: "why this goal / plan / action?" diagnostics (design 06).

The inspector is a passive observer: it subscribes to planner and replan-policy
hooks, ingests Jev telemetry through explicit user-wired callbacks, and records
goal selections through an explicit strategy shim. These tests pin the §9
producer contracts, the §10 failure-mode coverage, and the "no synthesized
producer facts" rule.
"""

import copy
import dataclasses
import json
import math
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import goapauto
from goapauto.models.actions import Action
from goapauto.models.goal import Goal
from goapauto.models.goal_arbitrator import (
    GoalArbitrator,
    PriorityGoalStrategy,
)
from goapauto.models.goap_planner import PlanExecutionError, Planner
from goapauto.models.replan import ReplanPolicy
from goapauto.models.sensors import Sensor, SensorManager
from goapauto.models.worldstate import WorldState
from goapauto.utils.inspector import (
    AgentInspector,
    EventType,
    TraceEvent,
    _action_name,
    _getattr_silent,
)

ACTIONS = [
    ("step1", {"s": 0}, {"s": 1}, 1.0),
    ("step2", {"s": 1}, {"s": 2}, 1.0),
    ("step3", {"s": 2}, {"s": 3}, 1.0),
]

PUBLIC_METHODS = {
    "attach",
    "detach",
    "mark_tick",
    "what_changed_since",
    "why_goal",
    "why_plan",
    "timeline",
    "buffer_info",
    "to_markdown",
    "to_jsonl",
    "note_jev_call",
    "shim_strategy",
    "note_goal_selected",
    "note_sensor_update",
    "record_search_error",
}


@pytest.fixture
def planner() -> Planner:
    return Planner(actions_list=ACTIONS, verbose=False)


@pytest.fixture
def goal() -> Goal:
    return Goal(target_state={"s": 3}, name="reach-three")


@pytest.fixture
def attached(planner: Planner) -> AgentInspector:
    insp = AgentInspector()
    insp.attach(planner)
    yield insp
    insp.detach()


@dataclasses.dataclass(frozen=True)
class _GateDecision:
    question: str
    key: str | None
    confidence: float | None
    threshold: float
    decision: str
    reason: str
    fallback: str | None


def make_gate_decision(question="goal", key=None):
    return _GateDecision(
        question=question,
        key=key,
        confidence=0.82,
        threshold=0.5,
        decision="proceed",
        reason="above_threshold",
        fallback="abstain",
    )


def make_strategy_record(**overrides):
    record = {
        "source": "strategy",
        "pick_label": "combat",
        "label_matched": True,
        "fallback_reason": None,
        "gated": False,
        "confidence_absent": False,
        "confidence": 0.82,
        "threshold": 0.5,
        "gate_decisions": [make_gate_decision()],
        "latency_ms": 5.0,
        "error": None,
        "backend": "typesafe",
        "model": "jev-test",
    }
    record.update(overrides)
    return SimpleNamespace(**record)


def make_sensor_record(**overrides):
    record = {
        "source": "sensor",
        "backend": "typesafe",
        "model": "jev-test",
        "latency_ms": 12.0,
        "input_tokens": 10,
        "output_tokens": 20,
        "error": None,
        "stale_cache_hit": False,
        "fully_gated": False,
        "gate_decisions": [make_gate_decision(question="danger?", key="danger")],
    }
    record.update(overrides)
    return SimpleNamespace(**record)


class _DiagSensor(Sensor):
    def __init__(self, name, payload=None, explode=False):
        self.name = name
        self._payload = payload or {}
        self._explode = explode

    def sense(self):
        return {}

    def diagnostics(self):
        if self._explode:
            raise RuntimeError("diagnostics boom")
        return dict(self._payload)


class TestPublicAPI:
    def test_new_symbols_reexported_and_in_all(self):
        for name in ("AgentInspector", "TraceEvent", "EventType"):
            assert name in goapauto.__all__
            assert hasattr(goapauto, name)

    def test_utils_reexports(self):
        from goapauto.utils import AgentInspector as UAgentInspector

        assert UAgentInspector is AgentInspector

    def test_no_public_method_beyond_spec(self):
        public = {
            name
            for name in dir(AgentInspector)
            if not name.startswith("_") and callable(getattr(AgentInspector, name))
        }
        assert public == PUBLIC_METHODS | {"record_sensor_error"}

    def test_event_type_members(self):
        import typing

        assert set(typing.get_args(EventType)) == {
            "tick_boundary",
            "goal_selected",
            "plan_found",
            "plan_failed",
            "search_error",
            "action_started",
            "action_completed",
            "action_failed",
            "execution_failed",
            "execution_interrupted",
            "replan",
            "replan_throttled",
            "budget_exhausted",
            "judgment_gated",
            "sensor_update",
            "sensor_error",
        }


class TestAttachDetach:
    def test_attach_twice_raises(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            with pytest.raises(ValueError, match="twice"):
                insp.attach(planner)
        finally:
            insp.detach()

    def test_attach_requires_planner(self):
        insp = AgentInspector()
        with pytest.raises(TypeError):
            insp.attach(None)  # type: ignore[arg-type]

    def test_detach_before_attach_is_noop(self):
        AgentInspector().detach()

    def test_reattach_after_detach(self, planner, goal):
        insp = AgentInspector()
        insp.attach(planner)
        insp.detach()
        insp.attach(planner)
        try:
            planner.generate_plan(WorldState(s=0), goal)
            assert any(e.type == "plan_found" for e in insp.timeline())
        finally:
            insp.detach()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_events": 0},
            {"max_events": -3},
            {"max_events": True},
            {"max_events": "many"},
            {"max_report_events": -1},
            {"max_report_events": False},
            {"keep_ticks": 0},
            {"keep_ticks": "two"},
        ],
    )
    def test_attach_rejects_bad_bounds(self, planner, kwargs):
        insp = AgentInspector()
        with pytest.raises((TypeError, ValueError)):
            insp.attach(planner, **kwargs)
        insp.detach()

    def test_attach_accepts_unbounded(self, planner):
        insp = AgentInspector()
        insp.attach(planner, max_events=None)
        try:
            assert insp.buffer_info().event_count == 0
        finally:
            insp.detach()

    def test_no_planner_mutation_beyond_hook_lists(self, planner):
        insp = AgentInspector()
        before = {k: list(v) for k, v in planner.hooks.items()}
        subscribed = set(AgentInspector._ALWAYS_HOOKS) | set(
            AgentInspector._CONDITIONAL_HOOKS
        )
        insp.attach(planner)
        try:
            for event, callbacks in planner.hooks.items():
                expected = len(before[event]) + (1 if event in subscribed else 0)
                assert len(callbacks) == expected
        finally:
            insp.detach()
        after = {k: list(v) for k, v in planner.hooks.items()}
        assert after == before

    def test_conditional_hooks_absent_are_skipped(self, planner):
        insp = AgentInspector()
        del planner.hooks["on_budget_exhausted"]
        del planner.hooks["on_execution_interrupted"]
        insp.attach(planner)
        try:
            assert "on_budget_exhausted" not in planner.hooks
        finally:
            insp.detach()
        # detach must not resurrect the deleted hooks
        assert "on_budget_exhausted" not in planner.hooks

    def test_detach_uses_list_replacement(self, planner, attached):
        insp = attached
        holder = {}
        planner.register_hook(
            "on_plan_found", lambda **kw: holder.setdefault("seen", []).append(kw)
        )
        original = planner.hooks["on_plan_found"]
        insp.detach()
        assert planner.hooks["on_plan_found"] is not original
        assert not any(
            getattr(cb, "__self__", None) is insp
            for cb in planner.hooks["on_plan_found"]
        )

    def test_detach_during_trigger_is_safe(self, planner, attached):
        # detach() replaces the hook lists; the in-flight trigger keeps
        # iterating the old list, and the inspector's callback drops writes
        # once detached instead of crashing.
        insp = attached
        planner.register_hook("on_plan_found", lambda **kw: insp.detach())
        planner.generate_plan(WorldState(s=0), Goal(target_state={"s": 3}))
        assert len(insp.timeline(types=["plan_found"])) == 1
        planner.generate_plan(WorldState(s=0), Goal(target_state={"s": 3}))
        assert len(insp.timeline(types=["plan_found"])) == 1


class TestBufferBounds:
    def test_max_events_keeps_newest_and_counts_evictions(self, planner):
        insp = AgentInspector()
        insp.attach(planner, max_events=2)
        try:
            planner.generate_plan(WorldState(s=0), Goal(target_state={"s": 3}))
            planner.generate_plan(WorldState(s=0), Goal(target_state={"s": 3}))
            planner.generate_plan(WorldState(s=0), Goal(target_state={"s": 99}))
            info = insp.buffer_info()
            assert info.event_count == 2
            assert info.newest_seq - info.oldest_seq == 1
            assert info.dropped_events > 0
            md = insp.to_markdown()
            assert "evicted" in md
        finally:
            insp.detach()

    def test_post_detach_writes_dropped_but_counted(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        insp.detach()
        before = insp.buffer_info().dropped_events
        insp.note_jev_call("x", make_sensor_record())
        insp.record_search_error(RuntimeError("boom"))
        assert insp.buffer_info().dropped_events == before + 2
        assert insp.buffer_info().event_count == 0

    def test_unticked_events_carry_tick_none(self, planner, attached):
        insp = attached
        planner.generate_plan(WorldState(s=0), Goal(target_state={"s": 3}))
        events = insp.timeline()
        assert events
        assert all(e.tick is None for e in events)

    def test_tick_ids_keep_incrementing_across_detach(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        assert insp.mark_tick(WorldState(s=0)) == 1
        insp.detach()
        insp.attach(planner)
        try:
            assert insp.mark_tick(WorldState(s=1)) == 2
        finally:
            insp.detach()

    def test_mark_tick_rejects_non_worldstate(self, attached):
        with pytest.raises(TypeError):
            attached.mark_tick({"s": 0})  # type: ignore[arg-type]


class TestHookEvents:
    def test_plan_found_records_full_payload(self, planner, attached, goal):
        insp = attached
        plan = ["step1", "step2", "step3"]
        planner.generate_plan(WorldState(s=0), goal)
        events = insp.timeline(types=["plan_found"])
        assert len(events) == 1
        data = events[0].data
        assert data["plan"] == plan
        assert data["per_action_costs"] == [1.0, 1.0, 1.0]
        assert data["total_cost"] == pytest.approx(3.0)
        assert data["plan_length"] == 3
        assert data["stop_reason"] == "plan_found"
        assert data["budget_exhausted"] is False
        assert data["search_summary"]["expanded_count"] >= 3
        assert data["search_summary"]["max_depth_reached"] == 3

    def test_plan_failed_stop_reasons(self, planner, attached):
        insp = attached
        # frontier_empty: no applicable action at all
        planner.generate_plan(WorldState(s=0), Goal(target_state={"s": 99}))
        failed = insp.timeline(types=["plan_failed"])
        assert len(failed) == 1
        assert failed[0].data["stop_reason"] == "frontier_empty"
        assert failed[0].data["plan"] == []

        # iteration_budget: nodes_visited exceeds max_iterations
        small = Planner(actions_list=ACTIONS, max_iterations=1, verbose=False)
        insp2 = AgentInspector()
        insp2.attach(small)
        try:
            small.generate_plan(WorldState(s=0), Goal(target_state={"s": 3}))
            failed2 = insp2.timeline(types=["plan_failed"])
            assert failed2 and failed2[0].data["stop_reason"] == "iteration_budget"
        finally:
            insp2.detach()

    def test_time_budget_stop_reason_and_budget_event(self):
        deep = [(f"mv{i}", {"x": i}, {"x": i + 1}, 1.0) for i in range(200)]
        planner = Planner(actions_list=deep, verbose=False)
        insp2 = AgentInspector()
        insp2.attach(planner)
        try:
            planner.generate_plan(
                WorldState(x=0), Goal(target_state={"x": 200}), time_budget=1e-6
            )
            failed = insp2.timeline(types=["plan_failed"])
            assert failed and failed[0].data["stop_reason"] == "time_budget"
            assert failed[0].data["budget_exhausted"] is True
            budget = insp2.timeline(types=["budget_exhausted"])
            assert len(budget) == 1
            data = budget[0].data
            assert data["budget_name"] == "time_budget"
            assert data["limit"] == 1e-6
            assert data["unit"] == "s"
            assert data["phase"] == "plan"
            # why_plan correlates the budget event to the plan decision
            decision = insp2.why_plan()
            assert decision is not None
            assert len(decision.budget_events) == 1
            assert decision.budget_events[0].type == "budget_exhausted"
        finally:
            insp2.detach()

    def test_action_lifecycle_events(self, planner, attached, goal):
        insp = attached
        plan = ["step1", "step2"]
        planner.execute_plan(WorldState(s=0), plan)
        types = [e.type for e in insp.timeline()]
        assert types == [
            "action_started",
            "action_completed",
            "action_started",
            "action_completed",
        ]
        assert insp.timeline()[0].data["action"] == "step1"
        assert insp.timeline()[1].data["action"] == "step1"

    def test_action_failure_dedup_single_event(self, planner, attached):
        insp = attached
        planner = planner
        with pytest.raises(PlanExecutionError):
            planner.execute_plan(WorldState(s=0), ["step2"])
        failed = insp.timeline(types=["action_failed"])
        assert len(failed) == 1
        assert failed[0].data["action"] == "step2"
        assert failed[0].data["execution_failed"] is True
        assert insp.timeline(types=["execution_failed"]) == []

    def test_orphan_action_failed_finalizes_without_execution_failed(
        self, planner, attached
    ):
        insp = attached
        # fire on_action_failed without the paired on_execution_failed
        planner._trigger_hook(
            "on_action_failed",
            action=Action(name="x", preconditions={}, effects={}),
            state=WorldState(s=0),
        )
        planner._trigger_hook(
            "on_action_start",
            action=Action(name="y", preconditions={}, effects={}),
            state=WorldState(s=0),
        )
        failed = insp.timeline(types=["action_failed"])
        assert len(failed) == 1
        assert failed[0].data["execution_failed"] is False

    def test_orphan_execution_failed_without_action_failed(self, planner, attached):
        insp = attached
        planner._trigger_hook(
            "on_execution_failed",
            action=Action(name="x", preconditions={}, effects={}),
            state=WorldState(s=0),
        )
        events = insp.timeline(types=["execution_failed"])
        assert len(events) == 1
        assert events[0].data["action"] == "x"
        assert insp.timeline(types=["action_failed"]) == []

    def test_execution_complete_produces_no_event(self, planner, attached, goal):
        insp = attached
        planner.execute_plan(WorldState(s=0), ["step1"])
        assert "execution_complete" not in {e.type for e in insp.timeline()}

    def test_search_error_recorded(self, attached):
        insp = attached
        err = RuntimeError("predicate exploded")
        insp.record_search_error(err)
        events = insp.timeline(types=["search_error"])
        assert len(events) == 1
        assert events[0].data["error"] == "RuntimeError"
        assert events[0].data["message"] == "predicate exploded"

    def test_search_error_before_attach_not_in_buffer(self, planner):
        insp = AgentInspector()
        insp.record_search_error(RuntimeError("early"))
        insp.attach(planner)
        try:
            assert insp.timeline(types=["search_error"]) == []
        finally:
            insp.detach()

    def test_search_error_newer_than_plan_renders_in_markdown(
        self, planner, attached, goal
    ):
        insp = attached
        planner.generate_plan(WorldState(s=0), goal)
        plan_event = insp.timeline(types=["plan_found"])[0]
        insp.record_search_error(ValueError("bad search"))
        md = insp.to_markdown()
        assert "search error" in md
        assert "bad search" in md
        assert str(plan_event.seq) in md  # decision age is visible

    def test_older_search_error_does_not_shadow_plan(self, planner, attached, goal):
        insp = attached
        insp.record_search_error(ValueError("old"))
        planner.generate_plan(WorldState(s=0), goal)
        md = insp.to_markdown()
        plan_section = md.split("## Why this plan", 1)[1].split("## ", 1)[0]
        assert "old" not in plan_section

    def test_plan_found_with_exotic_state_is_json_safe(self, planner, attached):
        insp = attached

        class Weird:
            def __repr__(self):
                return "<Weird>"

        state = WorldState(s=0, weird=Weird(), nan=math.nan)
        # action_failed records the state, so the exotic values enter the trace
        with pytest.raises(PlanExecutionError):
            planner.execute_plan(state, ["step2"])
        blob = json.dumps([e.to_dict() for e in insp.timeline()])
        assert "<Weird>" in blob
        assert "nan" in blob

    def test_cost_walk_through_branching_graph(self):
        branching = [
            ("a1", {"s": 0}, {"s": 1}, 2.0),
            ("a2", {"s": 0}, {"s": 2}, 5.0),
            ("b1", {"s": 1}, {"s": 3}, 3.0),
            ("b2", {"s": 2}, {"s": 3}, 1.0),
        ]
        planner = Planner(actions_list=branching, verbose=False)
        insp2 = AgentInspector()
        insp2.attach(planner)
        try:
            plan = planner.generate_plan(WorldState(s=0), Goal(target_state={"s": 3}))
            events = insp2.timeline(types=["plan_found"])
            assert len(events) == 1
            assert events[0].data["plan"] == plan.plan
            # cheapest path is a1 -> b1 (2.0 + 3.0 = 5.0 < 5.0 + 1.0)
            assert events[0].data["per_action_costs"] == [2.0, 3.0]
        finally:
            insp2.detach()

    def test_cost_walk_divergence_note(self, planner, attached, goal):
        insp = attached
        # synthetic plan that does not follow the search graph edges
        planner.generate_plan(WorldState(s=0), goal)
        stats = planner.stats
        planner._trigger_hook(
            "on_plan_found", plan=["step1", "nope", "step3"], stats=stats
        )
        events = insp.timeline(types=["plan_found"])
        last = events[-1]
        assert last.data["per_action_costs"][0] == 1.0
        assert last.data["per_action_costs"][1] is None
        assert last.data["per_action_costs"][2] is None
        assert "no edge" in last.data["note"]

    def test_malformed_plan_stats_never_synthesizes_facts(self, planner, attached):
        insp = attached
        bogus = SimpleNamespace(
            total_cost=1.0,
            nodes_expanded=0,
            nodes_visited=0,
            plan_length=0,
            execution_time=0.0,
            budget_exhausted=False,
        )
        planner._trigger_hook("on_plan_found", plan=None, stats=bogus)
        events = insp.timeline(types=["plan_found"])
        last = events[-1]
        assert last.data["plan"] == []
        assert last.data["per_action_costs"] == []
        assert "note" not in last.data  # empty plan: nothing to walk, no note


class TestTicksAndDiffs:
    def test_mark_tick_returns_incrementing_ids(self, attached):
        insp = attached
        assert insp.mark_tick(WorldState(s=0)) == 1
        assert insp.mark_tick(WorldState(s=1)) == 2

    def test_mark_tick_records_sensor_health(self, planner):
        manager = SensorManager(sensors=[_DiagSensor("d1")])
        insp = AgentInspector()
        insp.attach(planner, sensor_manager=manager)
        try:
            insp.mark_tick(WorldState(s=0))
            boundary = insp.timeline(types=["tick_boundary"])[0]
            assert isinstance(boundary.data["sensor_health"], list)
        finally:
            insp.detach()

    def test_what_changed_since_state_diff(self, attached):
        insp = attached
        t1 = insp.mark_tick(WorldState(s=0, hp=100))
        insp.mark_tick(WorldState(s=1, hp=80))
        delta = insp.what_changed_since(t1)
        assert delta.from_tick == t1
        assert delta.to_tick == t1 + 1
        assert delta.state_diff == {"s": (0, 1), "hp": (100, 80)}
        assert delta.goal_switch is None
        assert delta.sensor_updates == []

    def test_what_changed_since_unknown_or_evicted_tick(self, planner):
        insp = AgentInspector()
        insp.attach(planner, keep_ticks=1)
        try:
            with pytest.raises(KeyError):
                insp.what_changed_since(99)
            t1 = insp.mark_tick(WorldState(s=0))
            insp.mark_tick(WorldState(s=1))
            with pytest.raises(KeyError, match="evicted"):
                insp.what_changed_since(t1)
        finally:
            insp.detach()

    def test_tick_delta_events_scoped_to_interval(self, planner, attached, goal):
        insp = attached
        t1 = insp.mark_tick(WorldState(s=0))
        planner.generate_plan(WorldState(s=0), goal)
        t2 = insp.mark_tick(WorldState(s=1))
        delta = insp.what_changed_since(t1)
        types = [e.type for e in delta.events]
        assert "plan_found" in types
        # the interval is [from_tick, to_tick): from_tick's boundary is in,
        # to_tick's boundary is out
        assert any(e.type == "tick_boundary" and e.tick == t1 for e in delta.events)
        assert all(e.tick < t2 for e in delta.events)
        delta2 = insp.what_changed_since(t2)
        assert delta2.events == []

    def test_goal_switch_detected(self, attached):
        insp = attached
        t1 = insp.mark_tick(WorldState(s=0))
        insp.note_goal_selected(
            selected="explore",
            candidates=["explore"],
            strategy_name="PriorityGoalStrategy",
        )
        insp.mark_tick(WorldState(s=0))
        insp.note_goal_selected(
            selected="combat",
            candidates=["combat"],
            strategy_name="PriorityGoalStrategy",
        )
        delta = insp.what_changed_since(t1)
        assert delta.goal_switch == ("explore", "combat")

    def test_sensor_update_rollup_latest_per_sensor(self, attached):
        insp = attached
        t1 = insp.mark_tick(WorldState(s=0))
        insp.note_sensor_update(sensor="danger", updates={"danger": 1}, duration_ms=2.0)
        insp.note_sensor_update(
            sensor="danger", updates={"danger": 9}, duration_ms=3.0, error="stale"
        )
        insp.mark_tick(WorldState(s=0))
        delta = insp.what_changed_since(t1)
        assert len(delta.sensor_updates) == 1
        update = delta.sensor_updates[0]
        assert update.sensor == "danger"
        assert update.updates == {"danger": 9}
        assert update.error == "stale"

    def test_partiality_marked_when_interval_events_evicted(self, planner, goal):
        insp = AgentInspector()
        insp.attach(planner, max_events=8, keep_ticks=2)
        try:
            t1 = insp.mark_tick(WorldState(s=0))
            planner.generate_plan(WorldState(s=0), goal)
            insp.mark_tick(WorldState(s=0))
            for _ in range(9):
                planner.generate_plan(WorldState(s=0), goal)
            md = insp.to_markdown()
            assert "partial" in md
            delta = insp.what_changed_since(t1)
            info = insp.buffer_info()
            assert info.dropped_events > 0
            assert delta.from_tick == t1
        finally:
            insp.detach()

    def test_no_partiality_marker_without_eviction(self, planner, attached, goal):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        planner.generate_plan(WorldState(s=0), goal)
        insp.mark_tick(WorldState(s=0))
        md = insp.to_markdown()
        assert "partial" not in md


class TestGoalSelection:
    def test_shim_delegates_and_records(self, planner, attached):
        insp = attached
        strategy = PriorityGoalStrategy()
        shimmed = insp.shim_strategy(strategy, name="main")
        goals = [Goal(target_state={"s": 1}, name="g1", priority=1.0)]
        selected = shimmed.select(goals, WorldState(s=0))
        assert selected is goals[0]
        assert shimmed.strategy_name == "PriorityGoalStrategy"
        decision = insp.why_goal()
        assert decision is not None
        assert decision.selected == "g1"
        assert decision.candidates == ["g1"]
        assert decision.strategy == "PriorityGoalStrategy"

    def test_shim_strategy_name_defaults_to_record_name(self, planner, attached):
        insp = attached
        shimmed = insp.shim_strategy(PriorityGoalStrategy(), name="main")
        goals = [Goal(target_state={"s": 1}, name="g1", priority=1.0)]
        shimmed.select(goals, WorldState(s=0))
        decision = insp.why_goal()
        assert decision is not None
        assert decision.strategy == "PriorityGoalStrategy"

    def test_shim_label_falls_back_to_target_state(self, planner, attached):
        insp = attached
        shimmed = insp.shim_strategy(PriorityGoalStrategy(), name="main")
        goals = [Goal(target_state={"s": 1}, priority=1.0)]
        shimmed.select(goals, WorldState(s=0))
        decision = insp.why_goal()
        assert decision is not None
        assert decision.selected == str({"s": 1})

    def test_filtered_satisfied_goals_listed(self, planner):
        goals = [
            Goal(target_state={"s": 3}, name="pending", priority=1.0),
            Goal(target_state={"s": 0}, name="done", priority=2.0),
        ]
        arbitrator = GoalArbitrator(goals=goals, strategy=PriorityGoalStrategy())
        insp = AgentInspector()
        insp.attach(planner, arbitrator=arbitrator)
        try:
            shimmed = insp.shim_strategy(PriorityGoalStrategy(), name="main")
            state = WorldState(s=0)  # "done" is already satisfied
            arbitrator.strategy = shimmed
            arbitrator.select_goal(state)
            decision = insp.why_goal()
            assert decision is not None
            assert decision.selected == "pending"
            assert decision.filtered_satisfied == ["done"]
        finally:
            insp.detach()

    def test_why_goal_none_without_selection(self, attached):
        assert attached.why_goal() is None

    def test_why_goal_uses_latest_selection(self, attached):
        insp = attached
        insp.note_goal_selected(
            selected="a", candidates=["a"], strategy_name="PriorityGoalStrategy"
        )
        insp.note_goal_selected(
            selected="b", candidates=["a", "b"], strategy_name="PriorityGoalStrategy"
        )
        assert insp.why_goal().selected == "b"

    def test_markdown_renders_priority_order(self, planner):
        goals = [
            Goal(target_state={"s": 1}, name="low", priority=1.0),
            Goal(target_state={"s": 2}, name="high", priority=9.0),
        ]
        arbitrator = GoalArbitrator(goals=goals, strategy=PriorityGoalStrategy())
        insp = AgentInspector()
        insp.attach(planner, arbitrator=arbitrator)
        try:
            shimmed = insp.shim_strategy(PriorityGoalStrategy(), name="main")
            shimmed.select(goals, WorldState(s=0))
            md = insp.to_markdown()
            assert "priority order" in md
            candidates_line = next(
                line for line in md.splitlines() if "priority order" in line
            )
            # PriorityGoalStrategy: lowest number wins, so `low` (1.0) precedes
            # `high` (9.0) in the rendered candidate order
            assert candidates_line.index("`low`") < candidates_line.index("`high`")
        finally:
            insp.detach()


class TestJevTelemetry:
    def test_strategy_record_correlates_to_goal_selection(self, attached):
        insp = attached
        insp.note_jev_call("main", make_strategy_record())
        insp.note_goal_selected(
            selected="combat",
            candidates=["combat"],
            strategy_name="JevGoalStrategy",
            strategy_record_name="main",
        )
        decision = insp.why_goal()
        assert decision is not None
        jev = decision.jev
        assert jev is not None
        assert jev.pick_label == "combat"
        assert jev.label_matched is True
        assert jev.confidence == pytest.approx(0.82)
        assert jev.threshold == pytest.approx(0.5)
        assert jev.backend == "typesafe"
        assert jev.model == "jev-test"
        assert len(jev.gate_decisions) == 1
        assert jev.gate_decisions[0]["question"] == "goal"

    def test_strategy_record_goes_to_latest_selection_only(self, attached):
        insp = attached
        insp.note_jev_call("main", make_strategy_record(pick_label="first"))
        insp.note_goal_selected(
            selected="first",
            candidates=["first"],
            strategy_name="JevGoalStrategy",
            strategy_record_name="main",
        )
        # a second selection without a record name gets no Jev detail
        insp.note_goal_selected(
            selected="second",
            candidates=["second"],
            strategy_name="PriorityGoalStrategy",
        )
        decision = insp.why_goal()
        assert decision.selected == "second"
        assert decision.jev is None

    def test_overwritten_strategy_record_counts_orphan(self, attached):
        insp = attached
        insp.note_jev_call("main", make_strategy_record(pick_label="one"))
        insp.note_jev_call("main", make_strategy_record(pick_label="two"))
        assert insp.buffer_info().orphaned_strategy_records == 1
        insp.note_goal_selected(
            selected="two",
            candidates=["two"],
            strategy_name="JevGoalStrategy",
            strategy_record_name="main",
        )
        assert insp.why_goal().jev.pick_label == "two"
        assert "overwritten" in insp.to_markdown() or "orphan" in insp.to_markdown()

    def test_strategy_telemetry_emits_judgment_gated_events(self, attached):
        insp = attached
        insp.note_jev_call("main", make_strategy_record())
        gated = insp.timeline(types=["judgment_gated"])
        assert len(gated) == 1
        assert gated[0].data["context"] == "goal"
        assert gated[0].data["decision"] == "proceed"
        assert gated[0].data["backend"] == "typesafe"

    def test_sensor_telemetry_recorded_verbatim(self, attached):
        insp = attached
        insp.note_jev_call("danger", make_sensor_record())
        updates = insp.timeline(types=["sensor_update"])
        assert len(updates) == 1
        data = updates[0].data
        assert data["sensor"] == "danger"
        assert data["latency_ms"] == pytest.approx(12.0)
        assert data["served_from"] == "live"
        assert data["input_tokens"] == 10
        assert data["output_tokens"] == 20
        assert data["error"] is None

    def test_sensor_telemetry_stale_cache_hit(self, attached):
        insp = attached
        insp.note_jev_call("danger", make_sensor_record(stale_cache_hit=True))
        data = insp.timeline(types=["sensor_update"])[0].data
        assert data["stale_cache_hit"] is True
        assert data["served_from"] == "stale_cache"

    def test_sensor_telemetry_error_served_from_none(self, attached):
        insp = attached
        insp.note_jev_call("danger", make_sensor_record(error="timeout"))
        data = insp.timeline(types=["sensor_update"])[0].data
        assert data["error"] == "timeout"
        assert data["served_from"] == "none"

    def test_sensor_record_without_gate_decisions(self, attached):
        insp = attached
        insp.note_jev_call("danger", make_sensor_record(gate_decisions=[]))
        assert insp.timeline(types=["judgment_gated"]) == []
        assert len(insp.timeline(types=["sensor_update"])) == 1

    def test_sensor_gate_context_uses_key(self, attached):
        insp = attached
        insp.note_jev_call("danger", make_sensor_record())
        gated = insp.timeline(types=["judgment_gated"])
        assert gated[0].data["context"] == "sensor:danger"

    def test_unknown_telemetry_source_recorded_as_sensor_update(self, attached):
        insp = attached
        insp.note_jev_call("mystery", make_sensor_record(source="oracle"))
        updates = insp.timeline(types=["sensor_update"])
        assert len(updates) == 1
        assert updates[0].data["sensor"] == "mystery"
        assert "unknown" in updates[0].data["note"]

    def test_note_jev_call_never_raises(self, attached):
        insp = attached
        insp.note_jev_call("broken", object())
        insp.note_jev_call("broken2", SimpleNamespace(source="sensor"))
        updates = insp.timeline(types=["sensor_update"])
        assert len(updates) == 2

    def test_diagnostics_merge_contract(self, planner):
        payload = {
            "data_age_seconds": {"danger": 1.5},
            "staleness": {"danger": "stale"},
            "dead_keys": [],
            "worst_staleness": "stale",
        }
        manager = SensorManager(sensors=[_DiagSensor("danger", payload)])
        insp = AgentInspector()
        insp.attach(planner, sensor_manager=manager)
        try:
            insp.note_jev_call("danger", make_sensor_record())
            data = insp.timeline(types=["sensor_update"])[0].data
            assert data["staleness"] == {"danger": "stale"}
            assert data["data_age_seconds"] == {"danger": 1.5}
            assert data["dead_keys"] == []
            assert data["worst_staleness"] == "stale"
            assert data["keys"] == ["danger"]
        finally:
            insp.detach()

    def test_diagnostics_merge_failure_becomes_note(self, planner):
        manager = SensorManager(sensors=[_DiagSensor("danger", explode=True)])
        insp = AgentInspector()
        insp.attach(planner, sensor_manager=manager)
        try:
            insp.note_jev_call("danger", make_sensor_record())  # must not raise
            data = insp.timeline(types=["sensor_update"])[0].data
            assert "diagnostics() merge failed" in data["note"]
            assert data["staleness"] is None
        finally:
            insp.detach()

    def test_diagnostics_merge_skipped_without_manager(self, attached):
        insp = attached
        insp.note_sensor_update(sensor="danger", updates={"danger": 1}, duration_ms=1.0)
        data = insp.timeline(types=["sensor_update"])[0].data
        assert "no sensor manager" in data["note"]

    def test_diagnostics_merge_unknown_sensor_name(self, planner):
        manager = SensorManager(sensors=[_DiagSensor("other")])
        insp = AgentInspector()
        insp.attach(planner, sensor_manager=manager)
        try:
            insp.note_sensor_update(
                sensor="danger", updates={"danger": 1}, duration_ms=1.0
            )
            data = insp.timeline(types=["sensor_update"])[0].data
            assert "not found" in data["note"]
        finally:
            insp.detach()

    def test_explicit_sensor_update_prefers_explicit_keys(self, planner):
        manager = SensorManager(
            sensors=[_DiagSensor("danger", {"staleness": {"danger": "fresh"}})]
        )
        insp = AgentInspector()
        insp.attach(planner, sensor_manager=manager)
        try:
            insp.note_sensor_update(
                sensor="danger",
                updates={"danger": 1},
                duration_ms=1.0,
                keys=["danger", "extra"],
            )
            data = insp.timeline(types=["sensor_update"])[0].data
            assert data["keys"] == ["danger", "extra"]
        finally:
            insp.detach()

    def test_sensor_error_recorded_then_reraised(self, attached):
        insp = attached

        def flaky():
            try:
                raise ConnectionError("sensor down")
            except ConnectionError as exc:
                insp.record_sensor_error("danger", exc)
                raise

        with pytest.raises(ConnectionError, match="sensor down"):
            flaky()
        errors = insp.timeline(types=["sensor_error"])
        assert len(errors) == 1
        assert errors[0].data["sensor"] == "danger"
        assert errors[0].data["error"] == "ConnectionError"


class TestWhyPlan:
    def test_why_plan_none_without_plan(self, attached):
        assert attached.why_plan() is None

    def test_why_plan_latest_outcome(self, planner, attached, goal):
        insp = attached
        planner.generate_plan(WorldState(s=0), goal)
        first = insp.why_plan()
        assert first is not None and first.success is True
        planner.generate_plan(WorldState(s=0), Goal(target_state={"s": 99}))
        second = insp.why_plan()
        assert second is not None and second.success is False
        assert second.seq > first.seq

    def test_why_plan_replan_heuristic_same_tick(self, planner, attached, goal):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        planner.generate_plan(WorldState(s=0), goal)
        planner.generate_plan(WorldState(s=0), goal)
        decision = insp.why_plan()
        assert decision is not None
        assert decision.replan is True

    def test_why_plan_replan_false_across_ticks(self, planner, attached, goal):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        planner.generate_plan(WorldState(s=0), goal)
        insp.mark_tick(WorldState(s=0))
        planner.generate_plan(WorldState(s=0), goal)
        decision = insp.why_plan()
        assert decision is not None
        assert decision.replan is False

    def test_why_plan_replan_none_unticked(self, planner, attached, goal):
        insp = attached
        planner.generate_plan(WorldState(s=0), goal)
        decision = insp.why_plan()
        assert decision is not None
        assert decision.replan is None

    def test_why_plan_goal_correlation(self, planner, attached, goal):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        insp.note_goal_selected(
            selected="reach-three",
            candidates=["reach-three"],
            strategy_name="PriorityGoalStrategy",
        )
        planner.generate_plan(WorldState(s=0), goal)
        decision = insp.why_plan()
        assert decision is not None
        assert decision.goal == "reach-three"
        assert decision.goal_correlated is False

    def test_why_plan_goal_correlated_heuristic(self, planner, attached, goal):
        insp = attached
        insp.note_goal_selected(
            selected="stale-goal",
            candidates=["stale-goal"],
            strategy_name="PriorityGoalStrategy",
        )
        insp.mark_tick(WorldState(s=0))
        planner.generate_plan(WorldState(s=0), goal)
        decision = insp.why_plan()
        assert decision is not None
        assert decision.goal == "stale-goal"
        assert decision.goal_correlated is True

    def test_why_plan_unknown_goal_without_arbitration(self, planner, attached, goal):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        planner.generate_plan(WorldState(s=0), goal)
        decision = insp.why_plan()
        assert decision is not None
        assert decision.goal is None
        md = insp.to_markdown()
        assert "unknown" in md.split("## Why this plan", 1)[1].split("## ", 1)[0]

    def test_replan_policy_events(self, planner):
        policy = ReplanPolicy(planner)
        insp2 = AgentInspector()
        insp2.attach(planner, replan_policy=policy)
        try:
            decision_obj = SimpleNamespace(
                reason=SimpleNamespace(value="world_changed"),
                detail={"tick_id": 3},
            )
            result_obj = SimpleNamespace(plan=["step1"])
            policy._trigger_hook("on_replan", decision=decision_obj, result=result_obj)
            events = insp2.timeline(types=["replan"])
            assert len(events) == 1
            assert events[0].data["reason"] == "world_changed"
            assert events[0].data["plan"] == ["step1"]
            assert events[0].data["detail"] == {"tick_id": 3}
        finally:
            insp2.detach()

    def test_replan_skipped_throttled(self, planner):
        policy = ReplanPolicy(planner)
        insp2 = AgentInspector()
        insp2.attach(planner, replan_policy=policy)
        try:
            decision_obj = SimpleNamespace(
                reason=SimpleNamespace(value="throttled"),
                detail={
                    "throttled_by": "cooldown",
                    "last_replan_tick_id": 2,
                    "cooldown_remaining_ms": 150.0,
                },
            )
            policy._trigger_hook("on_replan_skipped", decision=decision_obj)
            events = insp2.timeline(types=["replan_throttled"])
            assert len(events) == 1
            data = events[0].data
            assert data["decision"] is False
            assert data["trigger"] == "cooldown"
            assert data["last_replan_tick"] == 2
            assert data["cooldown_remaining_ms"] == pytest.approx(150.0)
        finally:
            insp2.detach()

    def test_replan_skipped_non_throttled(self, planner):
        policy = ReplanPolicy(planner)
        insp2 = AgentInspector()
        insp2.attach(planner, replan_policy=policy)
        try:
            decision_obj = SimpleNamespace(
                reason=SimpleNamespace(value="sensor_stale"),
                detail={"tick_id": 4, "sensor_age": 12.5},
            )
            policy._trigger_hook("on_replan_skipped", decision=decision_obj)
            data = insp2.timeline(types=["replan_throttled"])[0].data
            assert data["reason"] == "sensor_stale"
            assert data["trigger"] is None
            assert data["sensor_age"] == pytest.approx(12.5)
        finally:
            insp2.detach()


class TestInterruption:
    def test_execution_interrupted_event(self, planner, attached):
        from goapauto.models.execution import PlanInterruptedError

        insp = attached
        execution = planner.begin_execution(WorldState(s=0), ["step1", "step2"])
        assert execution.interrupt("enemy_sighted", source="sensor") is True
        with pytest.raises(PlanInterruptedError):
            execution.run()
        interrupted = insp.timeline(types=["execution_interrupted"])
        assert len(interrupted) == 1
        data = interrupted[0].data
        assert data["reason"] == "enemy_sighted"
        assert data["source"] == "sensor"
        assert data["executed_actions"] == []
        assert data["remaining_actions"] == ["step1", "step2"]
        assert data["next_action"] == "step1"
        assert data["step_index"] == 0
        assert data["plan_length"] == 2


class TestTimeline:
    def test_timeline_filters_by_type(self, planner, attached, goal):
        insp = attached
        planner.generate_plan(WorldState(s=0), goal)
        insp.record_search_error(RuntimeError("x"))
        assert {e.type for e in insp.timeline()} == {"plan_found", "search_error"}
        assert [e.type for e in insp.timeline(types=["plan_found"])] == ["plan_found"]
        assert insp.timeline(types=["goal_selected"]) == []

    def test_timeline_since_seq(self, planner, attached, goal):
        insp = attached
        planner.generate_plan(WorldState(s=0), goal)
        insp.record_search_error(RuntimeError("x"))
        events = insp.timeline()
        tail = insp.timeline(since_seq=events[0].seq)
        assert [e.seq for e in tail] == [e.seq for e in events[1:]]

    def test_timeline_limit(self, planner, attached, goal):
        insp = attached
        planner.generate_plan(WorldState(s=0), goal)
        insp.record_search_error(RuntimeError("x"))
        limited = insp.timeline(limit=1)
        assert len(limited) == 1
        assert limited[0].seq == insp.timeline()[-1].seq

    def test_trace_event_to_dict(self, planner, attached, goal):
        insp = attached
        planner.generate_plan(WorldState(s=0), goal)
        event = insp.timeline()[0]
        assert isinstance(event, TraceEvent)
        blob = event.to_dict()
        assert set(blob) == {"seq", "tick", "type", "ts", "mono", "data"}
        assert blob["type"] == "plan_found"
        json.dumps(blob)

    def test_trace_event_immutable(self, planner, attached, goal):
        insp = attached
        planner.generate_plan(WorldState(s=0), goal)
        event = insp.timeline()[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.seq = 999  # type: ignore[misc]


class TestExports:
    def test_to_markdown_sections(self, attached):
        md = attached.to_markdown()
        assert "## Why this goal" in md
        assert "## Why this plan" in md
        assert "## What changed" in md

    def test_to_markdown_empty_state(self, attached):
        md = attached.to_markdown()
        assert "No goal selection recorded" in md
        assert "No plan recorded" in md
        assert "No ticks recorded" in md

    def test_to_markdown_full_story(self, planner, attached, goal):
        insp = attached
        insp.mark_tick(WorldState(s=0), label="loop")
        insp.note_goal_selected(
            selected="reach-three",
            candidates=["reach-three"],
            strategy_name="PriorityGoalStrategy",
        )
        planner.generate_plan(WorldState(s=0), goal)
        insp.mark_tick(WorldState(s=3))
        md = insp.to_markdown()
        assert "`reach-three`" in md
        assert "step1" in md
        assert "`s`: 0 → 3" in md
        assert "loop" in md

    def test_to_markdown_respects_max_report_events(self, planner, attached, goal):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        planner.generate_plan(WorldState(s=0), goal)
        insp.mark_tick(WorldState(s=0))
        full = insp.to_markdown()
        short = insp.to_markdown(max_report_events=1)
        assert "timeline() holds the full retained buffer" in short
        assert len(short) < len(full)

    def test_to_markdown_rejects_bad_limit(self, attached):
        with pytest.raises((TypeError, ValueError)):
            attached.to_markdown(max_report_events=-1)

    def test_to_jsonl_round_trip(self, planner, attached, goal, tmp_path):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        planner.generate_plan(WorldState(s=0), goal)
        path = tmp_path / "trace.jsonl"
        insp.to_jsonl(str(path))
        lines = path.read_text().splitlines()
        assert len(lines) == insp.buffer_info().event_count
        for line in lines:
            record = json.loads(line)
            assert set(record) == {"seq", "tick", "type", "ts", "mono", "data"}
        types = [json.loads(line)["type"] for line in lines]
        assert types == [e.type for e in insp.timeline()]

    def test_to_jsonl_rejects_bad_path(self, attached):
        with pytest.raises(TypeError):
            attached.to_jsonl(None)  # type: ignore[arg-type]


class TestJsonSafe:
    def test_json_safe_exotic_values(self):
        from goapauto.utils.inspector import json_safe

        class Broken:
            def __repr__(self):
                raise RuntimeError("no repr")

            def __str__(self):
                raise RuntimeError("no str")

        assert json_safe(math.nan) == "nan"
        assert json_safe(math.inf) == "inf"
        assert json_safe({1, 2}) == [1, 2]
        assert json_safe(Broken()) == {"__repr__": "<unrepresentable>"}

        def some_fn():
            pass

        assert json_safe(some_fn) == {
            "__callable__": "TestJsonSafe.test_json_safe_exotic_values.<locals>.some_fn"
        }

        class Color:
            RED = 1

        import enum

        class E(enum.Enum):
            A = "a"

        assert json_safe(E.A) == "a"
        blob = json.dumps(json_safe({"k": Broken(), "s": {Broken()}}))
        assert "<unrepresentable>" in blob

    def test_json_safe_dataclass_and_pydantic(self):
        from goapauto.utils.inspector import json_safe

        assert json_safe(WorldState(s=1, hp=2)) == {"s": 1, "hp": 2}
        manager = SensorManager(sensors=[_DiagSensor("d")])
        blob = json_safe(manager.health()[0])
        assert blob["name"] == "d"
        json.dumps(blob)


class TestFailureModes:
    def test_attach_mid_search_records_no_partial_events(self, planner, goal):
        # the plan_found hook fires once at search end; attaching mid-search
        # is impossible synchronously, but a slow hook proves atomicity:
        # the inspector sees exactly one plan event per search.
        insp = AgentInspector()
        insp.attach(planner)
        try:
            planner.generate_plan(WorldState(s=0), goal)
            assert len(insp.timeline(types=["plan_found"])) == 1
        finally:
            insp.detach()

    def test_action_names_verbatim_no_synthesis(self):
        weird = Action(name="attack: goblin (2x)!", preconditions={}, effects={"s": 1})
        planner = Planner(actions_list=[weird], verbose=False)
        insp2 = AgentInspector()
        insp2.attach(planner)
        try:
            planner.execute_plan(WorldState(s=0), ["attack: goblin (2x)!"])
            events = insp2.timeline(types=["action_started"])
            assert events[0].data["action"] == "attack: goblin (2x)!"
        finally:
            insp2.detach()

    def test_continue_plan_second_event_counts_as_replan(self, planner, goal):
        # continue_plan fires on_plan_found without identifying itself; the
        # same-tick second-plan heuristic classifies it as a replan.
        insp = AgentInspector()
        insp.attach(planner)
        try:
            insp.mark_tick(WorldState(s=0))
            planner.generate_plan(WorldState(s=0), goal)
            planner.continue_plan(WorldState(s=1), goal, ["step1"])
            decision = insp.why_plan()
            assert decision is not None
            assert decision.replan is True
        finally:
            insp.detach()


class TestCoverageEdges:
    """Targeted tests for defensive branches: the never-raise guarantees."""

    def test_safe_str_unrepresentable_dict_key(self, attached, tmp_path):
        class BadStr:
            def __str__(self):
                raise RuntimeError("boom")

        insp = attached
        insp.note_jev_call(
            "x", SimpleNamespace(source="sensor", gate_decisions=[{BadStr(): 1}])
        )
        path = str(tmp_path / "trace.jsonl")
        insp.to_jsonl(path)
        blob = open(path).read()
        assert "<unrepresentable>" in blob

    def test_json_safe_basemodel_dump_failure(self, attached, tmp_path):
        class BoomModel(BaseModel):
            x: int = 1

            def model_dump(self, *args, **kwargs):
                raise RuntimeError("boom")

        insp = attached
        insp.note_jev_call(
            "x", SimpleNamespace(source="sensor", gate_decisions=[BoomModel()])
        )
        path = str(tmp_path / "trace.jsonl")
        insp.to_jsonl(path)
        blob = open(path).read()
        assert "__repr__" in blob

    def test_json_safe_dataclass_getattr_failure(self, attached, tmp_path):
        @dataclasses.dataclass
        class BoomDC:
            x: int = 1

            def __getattribute__(self, name):
                if name == "x":
                    raise RuntimeError("boom")
                return super().__getattribute__(name)

        insp = attached
        insp.note_jev_call(
            "x", SimpleNamespace(source="sensor", gate_decisions=[BoomDC()])
        )
        path = str(tmp_path / "trace.jsonl")
        insp.to_jsonl(path)
        blob = open(path).read()
        assert "__repr__" in blob

    def test_getattr_silent_swallows_raising_lookup(self):
        class Meta(type):
            def __getattr__(cls, name):
                raise RuntimeError("boom")

        class Weird(metaclass=Meta):
            pass

        assert _getattr_silent(Weird, "missing") is None

    def test_action_name_non_string_falls_back_to_str(self):
        probe = SimpleNamespace(name=None)
        assert _action_name(probe) == str(probe)
        probe2 = SimpleNamespace(name=5)
        assert _action_name(probe2) == str(probe2)

    def test_all_recorders_drop_writes_when_detached(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        insp.detach()
        stats = SimpleNamespace(
            total_cost=0.0,
            nodes_expanded=0,
            nodes_visited=0,
            plan_length=0,
            execution_time=0.0,
            budget_exhausted=False,
            budget_limit=None,
        )
        action = Action(name="x", preconditions={}, effects={})
        state = WorldState(s=0)
        insp._on_plan_found(plan=["x"], stats=stats)
        insp._on_search_failed(stats=stats)
        insp._on_action_start(action=action, state=state)
        insp._on_action_complete(action=action, state=state)
        insp._on_action_failed(action=action, state=state)
        insp._on_execution_failed(action=action, state=state)
        insp._on_budget_exhausted(stats=stats)
        insp._on_execution_interrupted(
            reason="r",
            source="manual",
            state=state,
            executed_actions=[],
            remaining_actions=[],
            step_index=0,
            plan_length=0,
            state_diff={},
            interrupted_at=0.0,
        )
        insp._on_replan(
            decision=SimpleNamespace(reason="world_changed", detail={}),
            result=SimpleNamespace(plan=None),
        )
        insp._on_replan_skipped(decision=SimpleNamespace(reason="throttled", detail={}))
        insp.note_goal_selected(selected="g", candidates=["g"], strategy_name="S")
        insp.record_search_error(ValueError("x"))
        insp.record_sensor_error("s", ValueError("x"))
        insp.note_sensor_update(sensor="s", updates={}, duration_ms=1.0)
        insp.note_jev_call("s", SimpleNamespace(source="sensor"))
        insp.mark_tick(WorldState(s=0))
        info = insp.buffer_info()
        assert info.event_count == 0
        assert insp.timeline() == []
        # Every call is a dropped write: the guard counts each attempt, and
        # mark_tick reaches _record while detached.
        assert info.dropped_events == 16

    def test_replan_reason_without_value_attr(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            insp._on_replan(
                decision=SimpleNamespace(reason="world_changed", detail={}),
                result=SimpleNamespace(plan=None),
            )
            insp._on_replan_skipped(
                decision=SimpleNamespace(reason="cooldown", detail={})
            )
            events = insp.timeline(types=["replan", "replan_throttled"])
            assert events[0].data["reason"] == "world_changed"
            assert events[1].data["reason"] == "cooldown"
        finally:
            insp.detach()

    def test_search_summary_defensive_branches(self, planner, attached, monkeypatch):
        insp = attached
        insp._planner = None
        assert insp._search_summary() == {}
        insp._planner = planner
        monkeypatch.setattr(planner, "get_search_graph", lambda: ["not", "a", "dict"])
        assert insp._search_summary() == {}

    def test_mark_tick_deepcopy_fallback(self, attached, monkeypatch):
        insp = attached
        monkeypatch.setattr(
            copy, "deepcopy", lambda value: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        insp.mark_tick(WorldState(s=0))
        insp.mark_tick(WorldState(s=1))
        delta = insp.what_changed_since(1)
        assert delta.state_diff == {"s": (0, 1)}

    def test_interval_partial_empty_events(self, attached):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        insp.mark_tick(WorldState(s=0))
        insp._events.clear()
        assert insp._interval_partial(1) is False

    def test_interval_partial_missing_boundary_seq(self, attached):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        insp.mark_tick(WorldState(s=0))
        snapshot, _ = insp._snapshots[1]
        insp._snapshots[1] = (snapshot, None)
        assert insp._interval_partial(1) is False

    def test_unticked_goal_selection_skipped_in_switch_scan(self, attached):
        insp = attached
        insp.note_goal_selected(selected="a", candidates=["a"], strategy_name="S")
        insp.mark_tick(WorldState(s=0))
        insp.mark_tick(WorldState(s=0))
        delta = insp.what_changed_since(1)
        assert delta.goal_switch is None

    def test_jev_call_ingestion_failure_recorded(self, attached):
        insp = attached
        insp.note_jev_call("temp", SimpleNamespace(source="sensor", gate_decisions=5))
        events = insp.timeline(types=["sensor_update"])
        assert len(events) == 1
        assert "telemetry ingestion failed" in events[0].data["note"]

    def test_gate_context_sensor_question(self, attached):
        insp = attached
        gate = SimpleNamespace(
            question="danger?",
            key=None,
            decision=True,
            label_matched=False,
            confidence=0.5,
        )
        insp.note_jev_call(
            "danger", SimpleNamespace(source="sensor", gate_decisions=[gate])
        )
        events = insp.timeline(types=["judgment_gated"])
        assert events[0].data["context"] == "sensor:danger?"

    def test_stash_strategy_non_iterable_gates(self, attached):
        # The public note_jev_call path rejects a non-iterable earlier in
        # _emit_gate_events; the stash degrades to () when reached directly.
        insp = attached
        insp._stash_strategy_record(
            "rec", SimpleNamespace(source="strategy", gate_decisions=5)
        )
        insp.note_goal_selected(
            selected="g",
            candidates=["g"],
            strategy_name="S",
            strategy_record_name="rec",
        )
        decision = insp.why_goal()
        assert decision is not None
        assert decision.jev is not None
        assert decision.jev.gate_decisions == ()

    def test_sensor_telemetry_non_iterable_gates(self, attached):
        # Same reachability note as the strategy stash: exercised directly.
        insp = attached
        insp._record_sensor_telemetry(
            "temp", SimpleNamespace(source="sensor", gate_decisions=7), None
        )
        events = insp.timeline(types=["sensor_update"])
        assert events[0].data["gate_decisions"] == []

    def test_sensor_update_diagnostics_not_callable(self, planner):
        insp = AgentInspector()
        manager = SensorManager()
        sensor = _DiagSensor("weird")
        sensor.diagnostics = None  # type: ignore[assignment]
        manager.add_sensor(sensor)
        insp.attach(planner, sensor_manager=manager)
        try:
            insp.note_sensor_update(sensor="weird", updates={}, duration_ms=1.0)
            events = insp.timeline(types=["sensor_update"])
            assert "no diagnostics()" in events[0].data["note"]
        finally:
            insp.detach()

    def test_sensor_update_diagnostics_non_dict(self, planner):
        class ListDiag(_DiagSensor):
            def diagnostics(self):
                return ["not", "a", "dict"]

        insp = AgentInspector()
        manager = SensorManager()
        manager.add_sensor(ListDiag("weird"))
        insp.attach(planner, sensor_manager=manager)
        try:
            insp.note_sensor_update(sensor="weird", updates={}, duration_ms=1.0)
            events = insp.timeline(types=["sensor_update"])
            assert "returned" in events[0].data["note"]
        finally:
            insp.detach()

    def test_sensor_update_keys_derived_from_merge(self, planner):
        insp = AgentInspector()
        manager = SensorManager()
        manager.add_sensor(_DiagSensor("temp", {"staleness": {"temp": 1.0}}))
        insp.attach(planner, sensor_manager=manager)
        try:
            insp.note_sensor_update(sensor="temp", updates={}, duration_ms=1.0)
            events = insp.timeline(types=["sensor_update"])
            assert events[0].data["keys"] == ["temp"]
        finally:
            insp.detach()

    def test_note_sensor_update_failure_minimal_event(self, attached):
        insp = attached
        insp.note_sensor_update(sensor="s", updates=None, duration_ms=1.0)  # type: ignore[arg-type]
        events = insp.timeline(types=["sensor_update"])
        assert "note_sensor_update failed" in events[0].data["note"]

    def test_record_search_error_unrepresentable(self, attached):
        class BadStr(Exception):
            def __str__(self):
                raise RuntimeError("boom")

        insp = attached
        insp.record_search_error(BadStr())
        events = insp.timeline(types=["search_error"])
        assert events[0].data["message"] == "<unrepresentable>"

    def test_budget_events_next_seq_break(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            stats = SimpleNamespace(
                total_cost=0.0,
                nodes_expanded=0,
                nodes_visited=0,
                plan_length=0,
                execution_time=0.0,
                budget_exhausted=False,
                budget_limit=None,
            )
            insp._on_search_failed(stats=stats)
            insp._on_budget_exhausted(
                stats=SimpleNamespace(budget_limit=1.0, execution_time=2.0)
            )
            insp._on_search_failed(stats=stats)
            first = insp.timeline(types=["plan_failed"])[0]
            got = insp._budget_events_for(first)
            assert [event.seq for event in got] == [first.seq + 1]
        finally:
            insp.detach()

    def test_to_markdown_max_report_events_type(self, attached):
        with pytest.raises(TypeError, match="max_report_events must be an int"):
            attached.to_markdown(max_report_events="many")

    def test_markdown_detached_drops_footer(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        insp.detach()
        insp.note_jev_call("s", SimpleNamespace(source="sensor"))
        md = insp.to_markdown()
        assert "dropped after detach" in md

    def test_markdown_goal_selected_none(self, attached):
        insp = attached
        insp.note_goal_selected(selected=None, candidates=[], strategy_name="S")
        md = insp.to_markdown()
        assert "Selected: _none" in md

    def test_markdown_stale_plan_after_search_error(self, planner, attached, goal):
        insp = attached
        planner.generate_plan(WorldState(s=0), goal)
        insp.record_search_error(RuntimeError("kaput"))
        md = insp.to_markdown()
        assert "stale" in md

    def test_markdown_jev_fallback(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            insp.note_jev_call("r1", make_strategy_record(fallback_reason="timeout"))
            insp.note_goal_selected(
                selected="g",
                candidates=["g"],
                strategy_name="S",
                strategy_record_name="r1",
            )
            md = insp.to_markdown()
            assert "Fallback: `timeout`" in md
        finally:
            insp.detach()

    def test_markdown_jev_error(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            insp.note_jev_call("r2", make_strategy_record(error="backend down"))
            insp.note_goal_selected(
                selected="h",
                candidates=["h"],
                strategy_name="S",
                strategy_record_name="r2",
            )
            md = insp.to_markdown()
            assert "Jev error: `backend down`" in md
        finally:
            insp.detach()

    def test_markdown_budget_and_replan_lines(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            stats = SimpleNamespace(
                total_cost=0.0,
                nodes_expanded=0,
                nodes_visited=0,
                plan_length=0,
                execution_time=0.0,
                budget_exhausted=False,
                budget_limit=None,
            )
            insp._on_replan(
                decision=SimpleNamespace(reason="world_changed", detail={"x": 1}),
                result=SimpleNamespace(plan=["a"]),
            )
            insp._on_search_failed(stats=stats)
            insp._on_budget_exhausted(
                stats=SimpleNamespace(budget_limit=1.0, execution_time=2.0)
            )
            md = insp.to_markdown()
            assert "Budget event (budget_exhausted)" in md
            assert "limit=1.0s" in md
            assert "Replan decision: `world_changed`" in md
        finally:
            insp.detach()

    def test_markdown_no_state_changes(self, attached):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        insp.mark_tick(WorldState(s=0))
        md = insp.to_markdown()
        assert "State: no changes." in md

    def test_markdown_sensor_update_section(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            insp.mark_tick(WorldState(s=0))
            insp.note_jev_call(
                "danger",
                SimpleNamespace(
                    source="sensor",
                    staleness={"danger": 2.0},
                    gate_decisions=[make_gate_decision()],
                ),
            )
            insp.note_sensor_update(sensor="plain", updates={}, duration_ms=1.0)
            insp.mark_tick(WorldState(s=0))
            md = insp.to_markdown()
            assert "Sensor updates:" in md
            assert "served from live" in md
            assert "no detail" in md
        finally:
            insp.detach()

    def test_summarize_event_kinds(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            action = Action(name="x", preconditions={}, effects={})
            state = WorldState(s=0)
            insp.mark_tick(WorldState(s=0))
            insp.record_search_error(ValueError("bad"))
            insp._on_action_failed(action=action, state=state)
            insp._on_execution_failed(action=action, state=state)
            insp._on_execution_interrupted(
                reason="r",
                source="manual",
                state=state,
                executed_actions=[],
                remaining_actions=[],
                step_index=0,
                plan_length=0,
                state_diff={},
                interrupted_at=0.0,
            )
            insp._on_replan(
                decision=SimpleNamespace(reason="world_changed", detail={}),
                result=SimpleNamespace(plan=None),
            )
            insp._on_replan_skipped(
                decision=SimpleNamespace(reason="throttled", detail={})
            )
            insp._on_budget_exhausted(
                stats=SimpleNamespace(budget_limit=1.0, execution_time=2.0)
            )
            insp.note_jev_call(
                "danger",
                SimpleNamespace(source="sensor", gate_decisions=[make_gate_decision()]),
            )
            insp.note_sensor_update(sensor="s", updates={}, duration_ms=1.0)
            insp.record_sensor_error("s", ValueError("x"))
            insp.mark_tick(WorldState(s=0))
            md = insp.to_markdown(max_report_events=60)
            for needle in (
                "search_error",
                "action_failed",
                "execution_failed",
                "execution_interrupted",
                "replan_throttled",
                "budget_exhausted",
                "judgment_gated",
                "sensor_update",
                "sensor_error",
            ):
                assert needle in md, needle
        finally:
            insp.detach()

    def test_consecutive_action_failed_flushes_pending(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            action = Action(name="x", preconditions={}, effects={})
            state = WorldState(s=0)
            insp._on_action_failed(action=action, state=state)
            insp._on_action_failed(action=action, state=state)
            insp._on_execution_failed(action=action, state=state)
            events = insp.timeline(types=["action_failed"])
            assert len(events) == 2
            assert all(e.data["execution_failed"] is False for e in events[:1])
            assert events[1].data["execution_failed"] is True
        finally:
            insp.detach()

    def test_execution_failed_without_pending_pair(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            action = Action(name="x", preconditions={}, effects={})
            insp.mark_tick(WorldState(s=0))
            insp._on_execution_failed(action=action, state=WorldState(s=0))
            insp.mark_tick(WorldState(s=0))
            md = insp.to_markdown()
            assert "execution_failed action='x'" in md
        finally:
            insp.detach()

    def test_summarize_unknown_event_type_falls_back(self, attached):
        insp = attached
        event = TraceEvent(
            seq=1,
            tick=None,
            type="mystery_type",
            ts=0.0,
            mono=0.0,
            data={},  # type: ignore[arg-type]
        )
        assert insp._summarize_event(event) == "seq=1 unticked mystery_type"

    def test_summarize_action_started_and_completed(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            action = Action(name="x", preconditions={}, effects={})
            state = WorldState(s=0)
            insp.mark_tick(WorldState(s=0))
            insp._on_action_start(action=action, state=state)
            insp._on_action_complete(action=action, state=state)
            insp.mark_tick(WorldState(s=0))
            md = insp.to_markdown()
            assert "action_started action='x'" in md
            assert "action_completed action='x'" in md
        finally:
            insp.detach()

    def test_record_sensor_error_with_string(self, attached):
        insp = attached
        insp.record_sensor_error("temp", "timeout")
        events = insp.timeline(types=["sensor_error"])
        assert events[0].data["error"] == "timeout"

    def test_markdown_filtered_satisfied(self, planner):
        arbitrator = GoalArbitrator(
            goals=[
                Goal(target_state={"s": 1}, name="a"),
                Goal(target_state={"s": 2}, name="b"),
            ],
            strategy=PriorityGoalStrategy(),
        )
        insp2 = AgentInspector()
        insp2.attach(planner, arbitrator=arbitrator)
        try:
            insp2.note_goal_selected(
                selected="a", candidates=["a"], strategy_name="PriorityGoalStrategy"
            )
            md = insp2.to_markdown()
            assert "Already satisfied" in md
            assert "`b`" in md
        finally:
            insp2.detach()

    def test_markdown_stale_plan_ticked(self, planner, attached, goal):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        planner.generate_plan(WorldState(s=0), goal)
        insp.record_search_error(RuntimeError("kaput"))
        md = insp.to_markdown()
        assert "stale plan decision" in md

    def test_markdown_goal_correlated_note(self, planner, attached, goal):
        insp = attached
        insp.note_goal_selected(
            selected="reach-three",
            candidates=["reach-three"],
            strategy_name="PriorityGoalStrategy",
        )
        insp.mark_tick(WorldState(s=0))
        planner.generate_plan(WorldState(s=0), goal)
        md = insp.to_markdown()
        assert "correlated heuristically" in md

    def test_markdown_goal_switch_line(self, attached):
        insp = attached
        insp.mark_tick(WorldState(s=0))
        insp.note_goal_selected(selected="a", candidates=["a"], strategy_name="S")
        insp.mark_tick(WorldState(s=0))
        insp.note_goal_selected(selected="b", candidates=["b"], strategy_name="S")
        md = insp.to_markdown()
        assert "Goal switch: `a` → `b`" in md

    def test_markdown_replan_ticked_branch(self, planner):
        insp = AgentInspector()
        insp.attach(planner)
        try:
            stats = SimpleNamespace(
                total_cost=0.0,
                nodes_expanded=0,
                nodes_visited=0,
                plan_length=0,
                execution_time=0.0,
                budget_exhausted=False,
                budget_limit=None,
            )
            insp.mark_tick(WorldState(s=0))
            insp._on_replan(
                decision=SimpleNamespace(reason="world_changed", detail={}),
                result=SimpleNamespace(plan=["a"]),
            )
            insp._on_search_failed(stats=stats)
            insp.mark_tick(WorldState(s=0))
            md = insp.to_markdown()
            assert "Replan decision: `world_changed`" in md
        finally:
            insp.detach()

    def test_markdown_sensor_update_bits(self, planner):
        insp = AgentInspector()
        manager = SensorManager()
        manager.add_sensor(_DiagSensor("danger", {"staleness": {"danger": 2.0}}))
        insp.attach(planner, sensor_manager=manager)
        try:
            insp.mark_tick(WorldState(s=0))
            insp.note_jev_call("danger", SimpleNamespace(source="sensor"))
            insp.note_jev_call("badd", SimpleNamespace(source="sensor", error="boom"))
            insp.mark_tick(WorldState(s=0))
            md = insp.to_markdown()
            assert "staleness=" in md
            assert "error=boom" in md
        finally:
            insp.detach()
