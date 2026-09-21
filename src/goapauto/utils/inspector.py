"""Agent inspector: "why this goal / plan / action?" diagnostics.

The inspector is a passive observer over a planner, goal arbitrator, sensor
manager, and replan policy. It subscribes to planner and replan-policy hooks,
ingests Jev telemetry through explicit user-wired callbacks, and records goal
selections through an explicit strategy shim. It never mutates the objects it
observes: hook callbacks are removed by list replacement (never in-place
mutation), state snapshots are deep-copied, and every value entering the trace
is converted to JSON-safe data so the buffer is always serializable.
"""

import copy
import dataclasses
import enum
import json
import math
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel

from goapauto.models.goal import Goal
from goapauto.models.goal_arbitrator import GoalArbitrator, GoalSelectionStrategy
from goapauto.models.goap_planner import Planner
from goapauto.models.replan import ReplanPolicy
from goapauto.models.sensors import SensorManager
from goapauto.models.worldstate import WorldState

EventType = Literal[
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
]

__all__ = [
    "AgentInspector",
    "TraceEvent",
    "EventType",
    "GoalDecision",
    "PlanDecision",
    "TickDelta",
    "SensorUpdateInfo",
    "JevStrategyInfo",
    "BufferInfo",
]


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return "<unrepresentable>"


def _safe_str(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return "<unrepresentable>"


def json_safe(value: Any) -> Any:
    """Convert any value into JSON-serializable data without raising.

    Mappings keep their values (keys are stringified), sequences become lists,
    sets are sorted by repr, enums unwrap to their value, dataclasses and
    Pydantic models become field dicts, and anything else degrades to a
    ``{"__repr__": ...}`` or ``{"__callable__": ...}`` placeholder. Non-finite
    floats render as ``"nan"``/``"inf"``/``"-inf"`` strings.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return repr(value)
        return value
    if isinstance(value, enum.Enum):
        return json_safe(value.value)
    if isinstance(value, dict):
        return {_safe_str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [json_safe(item) for item in sorted(value, key=_safe_repr)]
    if isinstance(value, BaseModel):
        try:
            dumped = value.model_dump()
        except Exception:
            return {"__repr__": _safe_repr(value)}
        return json_safe(dumped)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            fields = {
                field.name: getattr(value, field.name)
                for field in dataclasses.fields(value)
            }
        except Exception:
            return {"__repr__": _safe_repr(value)}
        return {name: json_safe(item) for name, item in fields.items()}
    if callable(value):
        name = getattr(value, "__qualname__", None)
        return {"__callable__": name if isinstance(name, str) else _safe_repr(value)}
    return {"__repr__": _safe_repr(value)}


@dataclasses.dataclass(frozen=True)
class TraceEvent:
    """One retained trace record."""

    seq: int
    tick: int | None
    type: EventType
    ts: float
    mono: float
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return this event as a JSON-safe dict."""
        return {
            "seq": self.seq,
            "tick": self.tick,
            "type": self.type,
            "ts": self.ts,
            "mono": self.mono,
            "data": self.data,
        }


@dataclasses.dataclass(frozen=True)
class JevStrategyInfo:
    """Provider-agnostic Jev judgment detail for a strategy goal pick."""

    pick_label: str | None
    label_matched: bool | None
    fallback_reason: str | None
    gated: bool | None
    confidence_absent: bool | None
    confidence: float | None
    threshold: float | None
    gate_decisions: tuple[dict[str, Any], ...]
    latency_ms: float | None
    error: str | None
    backend: str | None
    model: str | None


@dataclasses.dataclass(frozen=True)
class GoalDecision:
    """The latest recorded goal arbitration outcome."""

    selected: str | None
    candidates: list[str]
    filtered_satisfied: list[str]
    strategy: str
    jev: JevStrategyInfo | None
    tick: int | None
    seq: int


@dataclasses.dataclass(frozen=True)
class PlanDecision:
    """The latest recorded plan outcome."""

    goal: str | None
    goal_correlated: bool
    plan: list[str]
    per_action_costs: list[float | None]
    total_cost: float
    nodes_expanded: int
    nodes_visited: int
    plan_length: int
    execution_time: float
    stop_reason: str
    budget_exhausted: bool
    search_summary: dict[str, Any]
    budget_events: list[TraceEvent]
    replan: bool | None
    success: bool
    tick: int | None
    seq: int


@dataclasses.dataclass(frozen=True)
class SensorUpdateInfo:
    """The latest sensor_update for one sensor inside a tick interval."""

    sensor: str | None
    tick: int | None
    seq: int
    updates: dict[str, Any]
    staleness: dict[str, Any]
    served_from: str | None
    error: str | None


@dataclasses.dataclass(frozen=True)
class TickDelta:
    """What changed between two retained tick snapshots."""

    from_tick: int
    to_tick: int
    state_diff: dict[str, tuple[Any, Any]]
    sensor_updates: list[SensorUpdateInfo]
    goal_switch: tuple[str | None, str | None] | None
    events: list[TraceEvent]


@dataclasses.dataclass(frozen=True)
class BufferInfo:
    """Trace buffer retention statistics."""

    oldest_seq: int
    newest_seq: int
    event_count: int
    dropped_events: int
    orphaned_strategy_records: int


def _getattr_silent(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _action_name(action: Any) -> str:
    name = _getattr_silent(action, "name")
    if isinstance(name, str):
        return name
    return _safe_str(action)


def _jev_info_to_dict(info: JevStrategyInfo) -> dict[str, Any]:
    return {
        "pick_label": info.pick_label,
        "label_matched": info.label_matched,
        "fallback_reason": info.fallback_reason,
        "gated": info.gated,
        "confidence_absent": info.confidence_absent,
        "confidence": info.confidence,
        "threshold": info.threshold,
        "gate_decisions": list(info.gate_decisions),
        "latency_ms": info.latency_ms,
        "error": info.error,
        "backend": info.backend,
        "model": info.model,
    }


class _StrategyShim:
    """Transparent recording decorator for a goal-selection strategy.

    Delegates ``select`` to the wrapped strategy, then records the outcome on
    the inspector. The wrapped strategy is exposed as ``inner``.
    """

    def __init__(
        self, inspector: "AgentInspector", strategy: GoalSelectionStrategy, name: str
    ) -> None:
        self._inspector = inspector
        self.inner = strategy
        self._record_name = name

    @property
    def strategy_name(self) -> str:
        """The wrapped strategy's class name (what the trace reports)."""
        return type(self.inner).__name__

    def select(self, goals: list[Goal], state: WorldState) -> Goal | None:
        selected = self.inner.select(goals, state)
        label = (
            selected.name
            if selected is not None and selected.name is not None
            else (_safe_str(selected.target_state) if selected is not None else None)
        )
        candidates = [
            goal.name if goal.name is not None else _safe_str(goal.target_state)
            for goal in goals
        ]
        self._inspector.note_goal_selected(
            selected=label,
            candidates=candidates,
            strategy_name=self.strategy_name,
            strategy_record_name=self._record_name,
        )
        return selected


class AgentInspector:
    """Passive "why this goal / plan / action?" diagnostics recorder."""

    _ALWAYS_HOOKS: tuple[str, ...] = (
        "on_plan_found",
        "on_search_failed",
        "on_action_start",
        "on_action_complete",
        "on_action_failed",
        "on_execution_failed",
    )
    _CONDITIONAL_HOOKS: tuple[str, ...] = (
        "on_budget_exhausted",
        "on_execution_interrupted",
    )

    def __init__(self) -> None:
        self._events: deque[TraceEvent] = deque()
        self._seq = 0
        self._tick = 0
        self._snapshots: dict[int, tuple[dict[str, Any], int | None]] = {}
        self._pending_strategy: dict[str, JevStrategyInfo] = {}
        self._pending_action_failed: dict[str, Any] | None = None
        self._dropped_events = 0
        self._evicted_events = 0
        self._orphaned_strategy_records = 0
        self._attached = False
        self._planner: Planner | None = None
        self._arbitrator: GoalArbitrator | None = None
        self._sensor_manager: SensorManager | None = None
        self._replan_policy: ReplanPolicy | None = None
        self._max_events: int | None = 4096
        self._max_report_events = 200
        self._keep_ticks = 2
        self._hook_subscriptions: list[
            tuple[dict[str, list[Callable[..., Any]]], str, Callable[..., Any]]
        ] = []
        self._policy_subscriptions: list[tuple[str, Callable[..., Any]]] = []

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def attach(
        self,
        planner: Planner,
        *,
        arbitrator: GoalArbitrator | None = None,
        sensor_manager: SensorManager | None = None,
        replan_policy: ReplanPolicy | None = None,
        max_events: int | None = 4096,
        max_report_events: int = 200,
        keep_ticks: int = 2,
    ) -> None:
        """Subscribe to planner and replan-policy hooks and start recording."""
        if self._attached:
            raise ValueError("AgentInspector.attach() called twice without detach()")
        if not isinstance(planner, Planner):
            raise TypeError(
                f"attach() requires a Planner, got {type(planner).__name__}"
            )
        self._validate_bounds(max_events, max_report_events, keep_ticks)
        self._max_events = max_events
        self._max_report_events = max_report_events
        self._keep_ticks = keep_ticks
        self._planner = planner
        self._arbitrator = arbitrator
        self._sensor_manager = sensor_manager
        self._replan_policy = replan_policy

        recorders: dict[str, Callable[..., Any]] = {
            "on_plan_found": self._on_plan_found,
            "on_search_failed": self._on_search_failed,
            "on_action_start": self._on_action_start,
            "on_action_complete": self._on_action_complete,
            "on_action_failed": self._on_action_failed,
            "on_execution_failed": self._on_execution_failed,
            "on_budget_exhausted": self._on_budget_exhausted,
            "on_execution_interrupted": self._on_execution_interrupted,
        }
        for event in self._ALWAYS_HOOKS:
            callback = recorders[event]
            planner.register_hook(event, callback)
            self._hook_subscriptions.append((planner.hooks, event, callback))
        for event in self._CONDITIONAL_HOOKS:
            if event in planner.hooks:
                callback = recorders[event]
                planner.hooks[event].append(callback)
                self._hook_subscriptions.append((planner.hooks, event, callback))
        if replan_policy is not None:
            for event, callback in (
                ("on_replan", self._on_replan),
                ("on_replan_skipped", self._on_replan_skipped),
            ):
                replan_policy.register_hook(event, callback)
                self._policy_subscriptions.append((event, callback))
        self._attached = True

    def detach(self) -> None:
        """Remove hook callbacks and stop recording; the buffer is retained."""
        if not self._attached:
            return
        for hooks_dict, event, callback in self._hook_subscriptions:
            hooks_dict[event] = [cb for cb in hooks_dict[event] if cb is not callback]
        self._hook_subscriptions = []
        if self._replan_policy is not None:
            for event, callback in self._policy_subscriptions:
                hooks = self._replan_policy.hooks
                hooks[event] = [cb for cb in hooks[event] if cb is not callback]
        self._policy_subscriptions = []
        self._attached = False
        self._pending_action_failed = None
        self._pending_strategy.clear()
        self._planner = None
        self._arbitrator = None
        self._sensor_manager = None
        self._replan_policy = None

    @staticmethod
    def _validate_bounds(
        max_events: int | None, max_report_events: int, keep_ticks: int
    ) -> None:
        if max_events is not None:
            if isinstance(max_events, bool) or not isinstance(max_events, int):
                raise TypeError(
                    f"max_events must be an int or None, got {type(max_events).__name__}"
                )
            if max_events < 1:
                raise ValueError(f"max_events must be >= 1, got {max_events}")
        for name, value, minimum in (
            ("max_report_events", max_report_events, 0),
            ("keep_ticks", keep_ticks, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int, got {type(value).__name__}")
            if value < minimum:
                raise ValueError(f"{name} must be >= {minimum}, got {value}")

    # ------------------------------------------------------------------
    # recording core
    # ------------------------------------------------------------------

    def _guard(self) -> bool:
        if not self._attached:
            self._dropped_events += 1
            return False
        return True

    def _record(self, type: EventType, data: dict[str, Any]) -> TraceEvent | None:
        if not self._attached:
            self._dropped_events += 1
            return None
        self._seq += 1
        event = TraceEvent(
            seq=self._seq,
            tick=self._tick if self._tick > 0 else None,
            type=type,
            ts=time.time(),
            mono=time.monotonic(),
            data=json_safe(data),
        )
        if self._max_events is not None:
            while len(self._events) >= self._max_events:
                self._events.popleft()
                self._dropped_events += 1
                self._evicted_events += 1
        self._events.append(event)
        return event

    # ------------------------------------------------------------------
    # planner hook recorders
    # ------------------------------------------------------------------

    def _on_plan_found(self, *, plan: Any, stats: Any) -> None:
        if not self._guard():
            return
        self._flush_pending_action_failed()
        plan_names = list(plan) if plan is not None else []
        costs, note = self._per_action_costs(plan_names)
        payload: dict[str, Any] = {
            "plan": plan_names,
            "per_action_costs": costs,
            "total_cost": stats.total_cost,
            "nodes_expanded": stats.nodes_expanded,
            "nodes_visited": stats.nodes_visited,
            "plan_length": stats.plan_length,
            "execution_time": stats.execution_time,
            "stop_reason": "plan_found",
            "budget_exhausted": False,
            "search_summary": self._search_summary(),
        }
        if note is not None:
            payload["note"] = note
        self._record("plan_found", payload)

    def _on_search_failed(self, *, stats: Any) -> None:
        if not self._guard():
            return
        self._flush_pending_action_failed()
        budget_exhausted = bool(getattr(stats, "budget_exhausted", False))
        planner = self._planner
        if budget_exhausted:
            stop_reason = "time_budget"
        elif planner is not None and stats.nodes_visited >= planner.max_iterations:
            stop_reason = "iteration_budget"
        else:
            stop_reason = "frontier_empty"
        self._record(
            "plan_failed",
            {
                "plan": [],
                "per_action_costs": [],
                "total_cost": stats.total_cost,
                "nodes_expanded": stats.nodes_expanded,
                "nodes_visited": stats.nodes_visited,
                "plan_length": stats.plan_length,
                "execution_time": stats.execution_time,
                "stop_reason": stop_reason,
                "budget_exhausted": budget_exhausted,
                "search_summary": self._search_summary(),
            },
        )

    def _on_action_start(self, *, action: Any, state: Any) -> None:
        if not self._guard():
            return
        self._flush_pending_action_failed()
        goal, _ = self._correlate_goal(self._tick if self._tick > 0 else None)
        self._record("action_started", {"action": _action_name(action), "goal": goal})

    def _on_action_complete(self, *, action: Any, state: Any) -> None:
        if not self._guard():
            return
        self._flush_pending_action_failed()
        goal, _ = self._correlate_goal(self._tick if self._tick > 0 else None)
        self._record("action_completed", {"action": _action_name(action), "goal": goal})

    def _on_action_failed(self, *, action: Any, state: Any) -> None:
        if not self._guard():
            return
        # The paired on_execution_failed finalizes this slot; it is NOT
        # flushed here, so the dedup window stays open across the pair.
        if self._pending_action_failed is not None:
            self._flush_pending_action_failed()
        self._pending_action_failed = {
            "action": _action_name(action),
            "state": state,
            "execution_failed": False,
        }

    def _on_execution_failed(self, *, action: Any, state: Any) -> None:
        if not self._guard():
            return
        pending = self._pending_action_failed
        self._pending_action_failed = None
        if pending is not None:
            pending["execution_failed"] = True
            self._record("action_failed", pending)
        else:
            self._record(
                "execution_failed",
                {"action": _action_name(action), "state": state},
            )

    def _on_budget_exhausted(self, *, stats: Any) -> None:
        if not self._guard():
            return
        self._flush_pending_action_failed()
        self._record(
            "budget_exhausted",
            {
                "budget_name": "time_budget",
                "limit": stats.budget_limit,
                "consumed": stats.execution_time,
                "unit": "s",
                "phase": "plan",
                "goal": None,
                "action": None,
            },
        )

    def _on_execution_interrupted(
        self,
        *,
        reason: Any,
        source: Any,
        state: Any,
        executed_actions: Any,
        remaining_actions: Any,
        step_index: Any,
        plan_length: Any,
        state_diff: Any,
        interrupted_at: Any,
    ) -> None:
        if not self._guard():
            return
        self._flush_pending_action_failed()
        remaining = list(remaining_actions)
        self._record(
            "execution_interrupted",
            {
                "reason": reason,
                "source": source,
                "state": state,
                "executed_actions": list(executed_actions),
                "remaining_actions": remaining,
                "step_index": step_index,
                "plan_length": plan_length,
                "state_diff": dict(state_diff),
                "interrupted_at": interrupted_at,
                "next_action": remaining[0] if remaining else None,
            },
        )

    def _on_replan(self, *, decision: Any, result: Any) -> None:
        if not self._guard():
            return
        reason = _getattr_silent(decision.reason, "value")
        if reason is None:
            reason = _safe_str(decision.reason)
        plan = _getattr_silent(result, "plan")
        self._record(
            "replan",
            {
                "reason": reason,
                "detail": _getattr_silent(decision, "detail"),
                "plan": list(plan) if plan is not None else None,
            },
        )

    def _on_replan_skipped(self, *, decision: Any) -> None:
        if not self._guard():
            return
        reason = _getattr_silent(decision.reason, "value")
        if reason is None:
            reason = _safe_str(decision.reason)
        detail = _getattr_silent(decision, "detail") or {}
        if reason == "throttled":
            payload = {
                "reason": reason,
                "decision": False,
                "trigger": detail.get("throttled_by"),
                "last_replan_tick": detail.get("last_replan_tick_id"),
                "cooldown_remaining_ms": detail.get("cooldown_remaining_ms"),
            }
        else:
            payload = {
                "reason": reason,
                "decision": False,
                "trigger": None,
                "last_replan_tick": detail.get("tick_id"),
                "cooldown_remaining_ms": None,
                "sensor_age": detail.get("sensor_age"),
            }
        self._record("replan_throttled", payload)

    def _flush_pending_action_failed(self) -> None:
        pending = self._pending_action_failed
        self._pending_action_failed = None
        if pending is not None:
            self._record("action_failed", pending)

    # ------------------------------------------------------------------
    # search-graph helpers
    # ------------------------------------------------------------------

    def _search_summary(self) -> dict[str, Any]:
        planner = self._planner
        if planner is None:
            return {}
        graph = planner.get_search_graph()
        if not isinstance(graph, dict):
            return {}
        metadata = graph.get("metadata")
        return dict(metadata) if isinstance(metadata, dict) else {}

    def _per_action_costs(
        self, plan: list[str]
    ) -> tuple[list[float | None], str | None]:
        costs: list[float | None] = []
        if not plan:
            return costs, None
        planner = self._planner
        graph = planner.get_search_graph() if planner is not None else {}
        nodes = graph.get("nodes", {}) if isinstance(graph, dict) else {}
        edges = graph.get("edges", []) if isinstance(graph, dict) else []
        root_id = None
        for node_id, node in nodes.items():
            if isinstance(node, dict) and node.get("parent") is None:
                root_id = node_id
                break
        current = root_id
        for index, name in enumerate(plan):
            match = None
            for edge in edges:
                if (
                    isinstance(edge, dict)
                    and edge.get("from") == current
                    and edge.get("action") == name
                ):
                    match = edge
                    break
            if match is None:
                costs.extend([None] * (len(plan) - index))
                return costs, (
                    f"cost walk stopped at step {index} ({name!r}): no edge from "
                    f"node {current!r} via {name!r}; remaining costs unknown"
                )
            costs.append(match.get("cost"))
            current = match.get("to")
        return costs, None

    def _correlate_goal(self, tick: int | None) -> tuple[str | None, bool]:
        same_tick: TraceEvent | None = None
        any_event: TraceEvent | None = None
        for event in self._events:
            if event.type != "goal_selected":
                continue
            any_event = event
            if event.tick == tick:
                same_tick = event
        if same_tick is not None:
            return same_tick.data.get("selected"), False
        if any_event is not None:
            return any_event.data.get("selected"), True
        return None, False

    # ------------------------------------------------------------------
    # ticks and diffs
    # ------------------------------------------------------------------

    def mark_tick(self, state: WorldState, label: str | None = None) -> int:
        """Record a tick boundary with a deep snapshot of the world state."""
        if not isinstance(state, WorldState):
            raise TypeError(
                f"mark_tick() requires a WorldState, got {type(state).__name__}"
            )
        health: list[Any] = []
        if self._sensor_manager is not None:
            health = [json_safe(entry) for entry in self._sensor_manager.health()]
        self._tick += 1
        tick_id = self._tick
        if self._attached:
            try:
                snapshot = copy.deepcopy(state.get_state())
            except Exception:
                snapshot = state.get_state()
            self._snapshots[tick_id] = (snapshot, None)
            while len(self._snapshots) > self._keep_ticks:
                oldest = next(iter(self._snapshots))
                del self._snapshots[oldest]
        event = self._record("tick_boundary", {"label": label, "sensor_health": health})
        if self._attached and event is not None and tick_id in self._snapshots:
            snapshot, _ = self._snapshots[tick_id]
            self._snapshots[tick_id] = (snapshot, event.seq)
        return tick_id

    def what_changed_since(self, tick_id: int) -> TickDelta:
        """Diff the world state and trace between a retained tick and now."""
        if tick_id not in self._snapshots:
            oldest = next(iter(self._snapshots), None)
            if (
                oldest is not None
                and isinstance(tick_id, int)
                and 1 <= tick_id < oldest
            ):
                raise KeyError(
                    f"tick {tick_id} snapshot was evicted; "
                    f"oldest retained tick is {oldest}"
                )
            raise KeyError(f"unknown tick id: {tick_id!r}")
        to_tick = next(reversed(self._snapshots))
        old_state, _ = self._snapshots[tick_id]
        new_state, _ = self._snapshots[to_tick]
        # WorldState.diff semantics: values from the old state first.
        state_diff = {
            key: (old_state.get(key), new_state.get(key))
            for key in old_state.keys() | new_state.keys()
            if old_state.get(key) != new_state.get(key)
        }
        latest_update: dict[Any, TraceEvent] = {}
        for event in self._events:
            if (
                event.type == "sensor_update"
                and event.tick is not None
                and tick_id <= event.tick < to_tick
            ):
                latest_update[event.data.get("sensor")] = event
        sensor_updates = [
            SensorUpdateInfo(
                sensor=event.data.get("sensor"),
                tick=event.tick,
                seq=event.seq,
                updates=event.data.get("updates") or {},
                staleness=event.data.get("staleness") or {},
                served_from=event.data.get("served_from"),
                error=event.data.get("error"),
            )
            for event in sorted(latest_update.values(), key=lambda e: e.seq)
        ]
        before: TraceEvent | None = None
        after: TraceEvent | None = None
        for event in self._events:
            if event.type != "goal_selected":
                continue
            if event.tick is None or event.tick > to_tick:
                continue
            if event.tick <= tick_id:
                before = event
            after = event
        goal_switch: tuple[str | None, str | None] | None = None
        if (
            before is not None
            and after is not None
            and before.data.get("selected") != after.data.get("selected")
        ):
            goal_switch = (before.data.get("selected"), after.data.get("selected"))
        events = [
            event
            for event in self._events
            if event.tick is not None and tick_id <= event.tick < to_tick
        ]
        return TickDelta(
            from_tick=tick_id,
            to_tick=to_tick,
            state_diff=state_diff,
            sensor_updates=sensor_updates,
            goal_switch=goal_switch,
            events=events,
        )

    def _interval_partial(self, from_tick: int) -> bool:
        """True when eviction may have removed events inside [from_tick, now)."""
        if not self._events:
            return False
        snapshot = self._snapshots.get(from_tick)
        boundary_seq = snapshot[1] if snapshot is not None else None
        if boundary_seq is None:
            return self._evicted_events > 0
        return self._events[0].seq > boundary_seq

    # ------------------------------------------------------------------
    # explicit ingestion APIs
    # ------------------------------------------------------------------

    def note_jev_call(self, name: str, record: Any) -> None:
        """Ingest one Jev/Judgment telemetry record. Never raises."""
        if not self._guard():
            return
        try:
            self._emit_gate_events(record)
            source = _getattr_silent(record, "source")
            if source == "strategy":
                self._stash_strategy_record(name, record)
            else:
                note = None
                if source != "sensor":
                    note = (
                        f"unknown telemetry source {source!r}; "
                        "recorded as sensor_update"
                    )
                self._record_sensor_telemetry(name, record, note)
        except Exception as exc:
            self._record(
                "sensor_update",
                {
                    "sensor": name,
                    "note": f"telemetry ingestion failed: {type(exc).__name__}: {exc}",
                },
            )

    def _emit_gate_events(self, record: Any) -> None:
        backend = _getattr_silent(record, "backend")
        model = _getattr_silent(record, "model")
        latency_ms = _getattr_silent(record, "latency_ms")
        error = _getattr_silent(record, "error")
        input_tokens = _getattr_silent(record, "input_tokens")
        output_tokens = _getattr_silent(record, "output_tokens")
        decisions = _getattr_silent(record, "gate_decisions") or ()
        for gate in decisions:
            question = _getattr_silent(gate, "question")
            key = _getattr_silent(gate, "key")
            if question == "goal":
                context: Any = "goal"
            elif key is not None:
                context = f"sensor:{key}"
            else:
                context = f"sensor:{question}"
            self._record(
                "judgment_gated",
                {
                    "question": question,
                    "key": key,
                    "confidence": _getattr_silent(gate, "confidence"),
                    "threshold": _getattr_silent(gate, "threshold"),
                    "decision": _getattr_silent(gate, "decision"),
                    "reason": _getattr_silent(gate, "reason"),
                    "fallback": _getattr_silent(gate, "fallback"),
                    "context": context,
                    "backend": backend,
                    "model": model,
                    "latency_ms": latency_ms,
                    "error": error,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            )

    def _stash_strategy_record(self, name: str, record: Any) -> None:
        if name in self._pending_strategy:
            self._orphaned_strategy_records += 1
        raw_decisions = _getattr_silent(record, "gate_decisions") or ()
        try:
            gate_decisions = tuple(json_safe(gate) for gate in raw_decisions)
        except TypeError:
            gate_decisions = ()
        self._pending_strategy[name] = JevStrategyInfo(
            pick_label=_getattr_silent(record, "pick_label"),
            label_matched=_getattr_silent(record, "label_matched"),
            fallback_reason=_getattr_silent(record, "fallback_reason"),
            gated=_getattr_silent(record, "gated"),
            confidence_absent=_getattr_silent(record, "confidence_absent"),
            confidence=_getattr_silent(record, "confidence"),
            threshold=_getattr_silent(record, "threshold"),
            gate_decisions=gate_decisions,
            latency_ms=_getattr_silent(record, "latency_ms"),
            error=_getattr_silent(record, "error"),
            backend=_getattr_silent(record, "backend"),
            model=_getattr_silent(record, "model"),
        )

    def _record_sensor_telemetry(
        self, name: str, record: Any, note: str | None = None
    ) -> None:
        stale_cache_hit = _getattr_silent(record, "stale_cache_hit")
        error = _getattr_silent(record, "error")
        if stale_cache_hit:
            served_from: Any = "stale_cache"
        elif error is None:
            served_from = "live"
        else:
            served_from = "none"
        merged, merge_note = self._diagnostics_merge(name)
        payload: dict[str, Any] = {
            "sensor": name,
            "latency_ms": _getattr_silent(record, "latency_ms"),
            "error": error,
            "stale_cache_hit": stale_cache_hit,
            "served_from": served_from,
            "input_tokens": _getattr_silent(record, "input_tokens"),
            "output_tokens": _getattr_silent(record, "output_tokens"),
            "backend": _getattr_silent(record, "backend"),
            "model": _getattr_silent(record, "model"),
            "keys": None,
            "staleness": None,
            "data_age_seconds": None,
            "dead_keys": None,
            "worst_staleness": None,
            "gate_decisions": None,
            "fully_gated": None,
        }
        raw_decisions = _getattr_silent(record, "gate_decisions") or ()
        try:
            payload["gate_decisions"] = [json_safe(gate) for gate in raw_decisions]
        except TypeError:
            payload["gate_decisions"] = []
        if merged is not None:
            payload["staleness"] = merged.get("staleness")
            payload["data_age_seconds"] = merged.get("data_age_seconds")
            payload["dead_keys"] = merged.get("dead_keys")
            payload["worst_staleness"] = merged.get("worst_staleness")
            payload["fully_gated"] = merged.get(
                "fully_gated_last_call", _getattr_silent(record, "fully_gated")
            )
            staleness_keys = payload["staleness"] or {}
            age_keys = payload["data_age_seconds"] or {}
            payload["keys"] = sorted(set(staleness_keys) | set(age_keys)) or None
        else:
            payload["fully_gated"] = _getattr_silent(record, "fully_gated")
        combined_note = note if note is not None else merge_note
        if combined_note is not None:
            payload["note"] = combined_note
        self._record("sensor_update", payload)

    def _diagnostics_merge(self, name: str) -> tuple[dict[str, Any] | None, str | None]:
        manager = self._sensor_manager
        if manager is None:
            return None, "diagnostics() merge skipped: no sensor manager attached"
        try:
            target = None
            for sensor in manager.sensors:
                sensor_name = _getattr_silent(sensor, "name") or type(sensor).__name__
                if sensor_name == name:
                    target = sensor
                    break
            if target is None:
                return (
                    None,
                    f"diagnostics() merge skipped: sensor {name!r} not found on "
                    "the attached sensor manager",
                )
            diagnostics = _getattr_silent(target, "diagnostics")
            if not callable(diagnostics):
                return (
                    None,
                    f"diagnostics() merge skipped: sensor {name!r} exposes "
                    "no diagnostics()",
                )
            merged = diagnostics()
            if not isinstance(merged, dict):
                return (
                    None,
                    f"diagnostics() merge skipped: diagnostics() returned "
                    f"{type(merged).__name__}, expected a dict",
                )
            return json_safe(merged), None
        except Exception as exc:
            return None, f"diagnostics() merge failed: {type(exc).__name__}: {exc}"

    def note_sensor_update(
        self,
        *,
        sensor: str,
        updates: dict[str, Any],
        duration_ms: float,
        keys: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        """Record one explicit sensor update. Never raises."""
        if not self._guard():
            return
        try:
            merged, note = self._diagnostics_merge(sensor)
            payload: dict[str, Any] = {
                "sensor": sensor,
                "keys": list(keys) if keys is not None else None,
                "updates": dict(updates),
                "duration_ms": duration_ms,
                "error": error,
                "staleness": None,
                "data_age_seconds": None,
                "dead_keys": None,
                "worst_staleness": None,
                "fully_gated": None,
            }
            if merged is not None:
                for field in (
                    "staleness",
                    "data_age_seconds",
                    "dead_keys",
                    "worst_staleness",
                ):
                    payload[field] = merged.get(field)
                payload["fully_gated"] = merged.get("fully_gated_last_call")
                if keys is None:
                    staleness_keys = payload["staleness"] or {}
                    age_keys = payload["data_age_seconds"] or {}
                    payload["keys"] = (
                        sorted(set(staleness_keys) | set(age_keys)) or None
                    )
            if note is not None:
                payload["note"] = note
            self._record("sensor_update", payload)
        except Exception as exc:
            self._record(
                "sensor_update",
                {
                    "sensor": sensor,
                    "note": f"note_sensor_update failed: {type(exc).__name__}: {exc}",
                },
            )

    def record_search_error(self, error: BaseException) -> None:
        """Record a search exception the caller re-raises after this call."""
        if not self._guard():
            return
        try:
            message = str(error)
        except Exception:
            message = "<unrepresentable>"
        self._record(
            "search_error", {"error": type(error).__name__, "message": message}
        )

    def record_sensor_error(self, sensor: str, error: BaseException | str) -> None:
        """Record a sensor exception the caller re-raises after this call."""
        if not self._guard():
            return
        if isinstance(error, BaseException):
            label = type(error).__name__
        else:
            label = _safe_str(error)
        self._record("sensor_error", {"sensor": sensor, "error": label})

    def shim_strategy(
        self, strategy: GoalSelectionStrategy, *, name: str
    ) -> _StrategyShim:
        """Wrap a goal-selection strategy so selections are recorded."""
        return _StrategyShim(self, strategy, name)

    def note_goal_selected(
        self,
        *,
        selected: str | None,
        candidates: list[str],
        strategy_name: str,
        strategy_record_name: str | None = None,
    ) -> None:
        """Record one goal arbitration outcome."""
        if not self._guard():
            return
        jev: dict[str, Any] | None = None
        if strategy_record_name is not None:
            info = self._pending_strategy.pop(strategy_record_name, None)
            if info is not None:
                jev = _jev_info_to_dict(info)
        filtered_satisfied: list[str] = []
        if self._arbitrator is not None:
            seen = set(candidates)
            for goal in self._arbitrator.goals:
                label = (
                    goal.name if goal.name is not None else _safe_str(goal.target_state)
                )
                if label not in seen:
                    filtered_satisfied.append(label)
        self._record(
            "goal_selected",
            {
                "selected": selected,
                "candidates": list(candidates),
                "filtered_satisfied": filtered_satisfied,
                "strategy": strategy_name,
                "jev": jev,
            },
        )

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def why_goal(self) -> GoalDecision | None:
        """Return the latest recorded goal arbitration outcome."""
        event: TraceEvent | None = None
        for candidate in self._events:
            if candidate.type == "goal_selected":
                event = candidate
        if event is None:
            return None
        data = event.data
        jev: JevStrategyInfo | None = None
        raw_jev = data.get("jev")
        if raw_jev is not None:
            fields = dict(raw_jev)
            fields["gate_decisions"] = tuple(fields.get("gate_decisions", ()))
            jev = JevStrategyInfo(**fields)
        return GoalDecision(
            selected=data.get("selected"),
            candidates=list(data.get("candidates", [])),
            filtered_satisfied=list(data.get("filtered_satisfied", [])),
            strategy=data.get("strategy", ""),
            jev=jev,
            tick=event.tick,
            seq=event.seq,
        )

    def why_plan(self) -> PlanDecision | None:
        """Return the latest recorded plan outcome."""
        event: TraceEvent | None = None
        for candidate in self._events:
            if candidate.type in ("plan_found", "plan_failed"):
                event = candidate
        if event is None:
            return None
        data = event.data
        goal, goal_correlated = self._correlate_goal(event.tick)
        if event.tick is None:
            replan: bool | None = None
        else:
            replan = any(
                other.type in ("plan_found", "plan_failed")
                and other.tick == event.tick
                and other.seq < event.seq
                for other in self._events
            )
        return PlanDecision(
            goal=goal,
            goal_correlated=goal_correlated,
            plan=list(data.get("plan", [])),
            per_action_costs=list(data.get("per_action_costs", [])),
            total_cost=data.get("total_cost", 0.0),
            nodes_expanded=data.get("nodes_expanded", 0),
            nodes_visited=data.get("nodes_visited", 0),
            plan_length=data.get("plan_length", 0),
            execution_time=data.get("execution_time", 0.0),
            stop_reason=data.get("stop_reason", ""),
            budget_exhausted=bool(data.get("budget_exhausted", False)),
            search_summary=dict(data.get("search_summary", {})),
            budget_events=self._budget_events_for(event),
            replan=replan,
            success=(event.type == "plan_found"),
            tick=event.tick,
            seq=event.seq,
        )

    def _budget_events_for(self, plan_event: TraceEvent) -> list[TraceEvent]:
        wanted = ("budget_exhausted", "replan_throttled")
        if plan_event.tick is not None:
            return [
                event
                for event in self._events
                if event.type in wanted and event.tick == plan_event.tick
            ]
        previous = 0
        for event in self._events:
            if (
                event.type in ("plan_found", "plan_failed")
                and event.seq < plan_event.seq
            ):
                previous = event.seq
        next_seq: int | None = None
        for event in self._events:
            if (
                event.type in ("plan_found", "plan_failed")
                and event.seq > plan_event.seq
            ):
                next_seq = event.seq
                break
        # The producer fires on_search_failed before on_budget_exhausted, so
        # the budget event lands *after* the plan event when unticked.
        return [
            event
            for event in self._events
            if event.type in wanted
            and previous < event.seq
            and (next_seq is None or event.seq < next_seq)
        ]

    def timeline(
        self,
        *,
        types: list[EventType] | None = None,
        since_seq: int = 0,
        limit: int | None = None,
    ) -> list[TraceEvent]:
        """Return retained events, newest last, with optional filters."""
        events = [
            event
            for event in self._events
            if (types is None or event.type in types) and event.seq > since_seq
        ]
        if limit is not None:
            events = events[-limit:] if limit > 0 else []
        return events

    def buffer_info(self) -> BufferInfo:
        """Return trace buffer retention statistics."""
        if self._events:
            oldest_seq = self._events[0].seq
            newest_seq = self._events[-1].seq
        else:
            oldest_seq = 0
            newest_seq = 0
        return BufferInfo(
            oldest_seq=oldest_seq,
            newest_seq=newest_seq,
            event_count=len(self._events),
            dropped_events=self._dropped_events,
            orphaned_strategy_records=self._orphaned_strategy_records,
        )

    # ------------------------------------------------------------------
    # exports
    # ------------------------------------------------------------------

    def to_markdown(self, max_report_events: int | None = None) -> str:
        """Render the why-report as Markdown."""
        if max_report_events is None:
            limit = self._max_report_events
        elif isinstance(max_report_events, bool) or not isinstance(
            max_report_events, int
        ):
            raise TypeError(
                "max_report_events must be an int, "
                f"got {type(max_report_events).__name__}"
            )
        elif max_report_events < 0:
            raise ValueError(f"max_report_events must be >= 0, got {max_report_events}")
        else:
            limit = max_report_events
        lines = ["# Agent inspector — why report", ""]
        lines.extend(self._render_goal_section())
        lines.extend(self._render_plan_section())
        lines.extend(self._render_changed_section(limit))
        lines.append("")
        info = self.buffer_info()
        if self._evicted_events:
            lines.append(
                f"_…{self._evicted_events} earlier event(s) evicted from the "
                "trace buffer._"
            )
        detached_drops = info.dropped_events - self._evicted_events
        if detached_drops:
            lines.append(f"_…{detached_drops} event write(s) dropped after detach._")
        if info.orphaned_strategy_records:
            lines.append(
                f"_…{info.orphaned_strategy_records} telemetry record(s) "
                "overwritten before a selection consumed them._"
            )
        return "\n".join(lines)

    def to_jsonl(self, path: str) -> None:
        """Write one JSON object per retained event to ``path``."""
        if not isinstance(path, str):
            raise TypeError(f"path must be a str, got {type(path).__name__}")
        with open(path, "w", encoding="utf-8") as handle:
            for event in self._events:
                handle.write(json.dumps(event.to_dict()) + "\n")

    # ------------------------------------------------------------------
    # markdown rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _short(value: Any, width: int = 80) -> str:
        text = _safe_repr(value)
        return text if len(text) <= width else text[: width - 1] + "…"

    def _render_goal_section(self) -> list[str]:
        lines = ["## Why this goal", ""]
        decision = self.why_goal()
        if decision is None:
            lines.append("No goal selection recorded yet.")
            lines.append("")
            return lines
        if decision.selected is None:
            lines.append("Selected: _none (the strategy returned None)_")
        else:
            lines.append(f"Selected: `{decision.selected}`")
        lines.append(
            f"Strategy: `{decision.strategy}` (tick {decision.tick}, "
            f"seq {decision.seq})"
        )
        if decision.strategy == "PriorityGoalStrategy":
            ordered = self._priority_ordered(decision.candidates)
            rendered = ", ".join(
                f"`{name}` ← selected" if name == decision.selected else f"`{name}`"
                for name in ordered
            )
            lines.append(f"Candidates in priority order: {rendered}")
        else:
            rendered = ", ".join(
                f"`{name}` ← selected" if name == decision.selected else f"`{name}`"
                for name in decision.candidates
            )
            lines.append(f"Candidates ({len(decision.candidates)}): {rendered}")
        if decision.filtered_satisfied:
            names = ", ".join(f"`{name}`" for name in decision.filtered_satisfied)
            lines.append(f"Already satisfied (filtered before arbitration): {names}")
        if decision.jev is not None:
            lines.extend(self._render_jev(decision.jev))
        lines.append("")
        return lines

    def _priority_ordered(self, candidates: list[str]) -> list[str]:
        if self._arbitrator is None:
            return list(candidates)
        priorities: dict[str, float] = {}
        for goal in self._arbitrator.goals:
            label = goal.name if goal.name is not None else _safe_str(goal.target_state)
            priorities[label] = goal.priority
        return sorted(candidates, key=lambda name: priorities.get(name, 0.0))

    def _render_jev(self, jev: JevStrategyInfo) -> list[str]:
        lines = []
        pick = f"`{jev.pick_label}`" if jev.pick_label is not None else "_unknown_"
        lines.append(f"Jev pick: {pick} (label_matched={jev.label_matched})")
        if jev.fallback_reason is not None:
            lines.append(f"Fallback: `{jev.fallback_reason}`")
        if jev.confidence is not None:
            lines.append(
                f"Confidence: {jev.confidence} (threshold {jev.threshold}, "
                f"gated={jev.gated}, confidence_absent={jev.confidence_absent})"
            )
        backend = f"`{jev.backend}`" if jev.backend else "_unknown (pre-05 record)_"
        model = f"`{jev.model}`" if jev.model else "_unknown_"
        latency = f"{jev.latency_ms} ms" if jev.latency_ms is not None else "_unknown_"
        lines.append(f"Backend: {backend}, model {model}, latency {latency}")
        if jev.error is not None:
            lines.append(f"Jev error: `{jev.error}`")
        for gate in jev.gate_decisions:
            lines.append(
                f"  - gate `{gate.get('question')}`: {gate.get('decision')} "
                f"(confidence {gate.get('confidence')} ≥ "
                f"{gate.get('threshold')}; {gate.get('reason')})"
            )
        return lines

    def _render_plan_section(self) -> list[str]:
        lines = ["## Why this plan", ""]
        plan_event: TraceEvent | None = None
        search_error: TraceEvent | None = None
        for event in self._events:
            if event.type in ("plan_found", "plan_failed"):
                plan_event = event
            elif event.type == "search_error":
                search_error = event
        if search_error is not None and (
            plan_event is None or search_error.seq > plan_event.seq
        ):
            data = search_error.data
            lines.append(
                f"⚠ The most recent planning outcome is a search error "
                f"(seq {search_error.seq}, tick {search_error.tick}):"
            )
            lines.append(f"  {data.get('error')}: {data.get('message')}")
            if plan_event is not None:
                lines.append(
                    f"  (Last plan decision is stale: seq {plan_event.seq}, "
                    f"tick {plan_event.tick}.)"
                )
            lines.append("")
        decision = self.why_plan()
        if decision is None:
            lines.append("No plan recorded yet.")
            lines.append("")
            return lines
        if decision.tick is not None and (
            search_error is not None and search_error.seq > decision.seq
        ):
            lines.append(
                f"_Showing the stale plan decision (seq {decision.seq}, "
                f"tick {decision.tick}) for age reference._"
            )
        if decision.goal is None:
            lines.append(
                "Goal: _unknown (no arbitration recorded — the plan may have "
                "been generated without the arbitrator)_"
            )
        else:
            lines.append(f"Goal: `{decision.goal}` (tick {decision.tick})")
            if decision.goal_correlated:
                lines.append(
                    "_Goal correlated heuristically from an earlier tick — call "
                    "`mark_tick` once per loop iteration for exact attribution._"
                )
        actions = " → ".join(f"`{name}`" for name in decision.plan)
        lines.append(
            f"Plan ({decision.plan_length} actions, total cost "
            f"{decision.total_cost}): {actions or '_none_'}"
        )
        lines.append(f"Per-action costs: {decision.per_action_costs}")
        lines.append(f"Stop reason: `{decision.stop_reason}`")
        summary = decision.search_summary
        lines.append(
            f"Search: {summary.get('expanded_count')} nodes expanded, "
            f"{summary.get('visited_count')} visited, "
            f"{decision.execution_time:.4f} s, max depth "
            f"{summary.get('max_depth_reached')}"
        )
        if decision.replan is None:
            lines.append("Replan: _unknown (no ticks recorded)_")
        else:
            lines.append(f"Replan: {'yes' if decision.replan else 'no'}")
        for event in decision.budget_events:
            data = event.data
            lines.append(
                f"Budget event ({event.type}): {data.get('budget_name') or ''}"
                f"limit={data.get('limit')}{data.get('unit') or ''} "
                f"consumed={data.get('consumed')}"
            )
        replan_event = self._latest_replan_for(decision)
        if replan_event is not None:
            data = replan_event.data
            lines.append(
                f"Replan decision: `{data.get('reason')}` — {data.get('detail')}"
            )
        outcome = "plan found ✓" if decision.success else "no plan ✗"
        lines.append(f"Outcome: {outcome}")
        lines.append("")
        return lines

    def _latest_replan_for(self, decision: PlanDecision) -> TraceEvent | None:
        latest: TraceEvent | None = None
        for event in self._events:
            if event.type != "replan":
                continue
            if decision.tick is not None:
                if event.tick == decision.tick and event.seq < decision.seq:
                    latest = event
            elif event.seq < decision.seq:
                latest = event
        return latest

    def _render_changed_section(self, limit: int) -> list[str]:
        lines = ["## What changed", ""]
        ticks = list(self._snapshots)
        if not ticks:
            lines.append("No ticks recorded yet — call mark_tick(state) each loop.")
            lines.append("")
            return lines
        if len(ticks) == 1:
            tick = ticks[0]
            lines.append(
                f"Only one tick recorded (tick {tick}) — no baseline to diff against."
            )
            boundary = next(
                (event for event in self._events if event.type == "tick_boundary"),
                None,
            )
            if boundary is not None:
                health = boundary.data.get("sensor_health") or []
                lines.append(f"Sensor health at tick {tick}: {len(health)} sensor(s).")
            lines.append("Events since attach:")
            events = [
                event
                for event in self._events
                if event.tick is None or event.tick <= tick
            ]
            lines.extend(self._render_event_list(events, limit))
            lines.append("")
            return lines
        delta = self.what_changed_since(ticks[-2])
        lines.append(f"Since tick {delta.from_tick} → tick {delta.to_tick}:")
        if delta.state_diff:
            lines.append("State diff:")
            for key in sorted(delta.state_diff, key=_safe_str):
                old, new = delta.state_diff[key]
                lines.append(f"  - `{key}`: {self._short(old)} → {self._short(new)}")
        else:
            lines.append("State: no changes.")
        if delta.goal_switch is not None:
            old_goal, new_goal = delta.goal_switch
            lines.append(f"Goal switch: `{old_goal}` → `{new_goal}`")
        if delta.sensor_updates:
            lines.append("Sensor updates:")
            for update in delta.sensor_updates:
                bits = []
                if update.served_from is not None:
                    bits.append(f"served from {update.served_from}")
                if update.staleness:
                    bits.append(f"staleness={self._short(update.staleness)}")
                if update.error is not None:
                    bits.append(f"error={update.error}")
                detail = "; ".join(bits) if bits else "no detail"
                lines.append(
                    f"  - `{update.sensor}` (tick {update.tick}, seq {update.seq}): "
                    f"{detail}"
                )
        lines.append("Events:")
        lines.extend(self._render_event_list(delta.events, limit))
        if self._interval_partial(delta.from_tick):
            info = self.buffer_info()
            lines.append(
                f"_Listing may be partial: events before seq {info.oldest_seq} "
                "were evicted from the trace buffer._"
            )
        lines.append("")
        return lines

    def _render_event_list(self, events: list[TraceEvent], limit: int) -> list[str]:
        shown = events[:limit]
        lines = [f"  - {self._summarize_event(event)}" for event in shown]
        remaining = len(events) - len(shown)
        if remaining > 0:
            lines.append(
                f"  - …and {remaining} more (timeline() holds the full retained buffer)"
            )
        return lines

    def _summarize_event(self, event: TraceEvent) -> str:
        data = event.data
        tick = f"tick={event.tick}" if event.tick is not None else "unticked"
        base = f"seq={event.seq} {tick} {event.type}"
        kind = event.type
        if kind == "tick_boundary":
            return f"{base} label={data.get('label')!r}"
        if kind == "goal_selected":
            return f"{base} selected={data.get('selected')!r}"
        if kind in ("plan_found", "plan_failed"):
            return (
                f"{base} plan={data.get('plan')} stop_reason={data.get('stop_reason')}"
            )
        if kind == "search_error":
            return f"{base} error={data.get('error')}: {data.get('message')}"
        if kind in ("action_started", "action_completed"):
            return f"{base} action={data.get('action')!r}"
        if kind == "action_failed":
            return (
                f"{base} action={data.get('action')!r} "
                f"execution_failed={data.get('execution_failed')}"
            )
        if kind == "execution_failed":
            return f"{base} action={data.get('action')!r}"
        if kind == "execution_interrupted":
            return f"{base} reason={data.get('reason')!r} source={data.get('source')!r}"
        if kind == "replan":
            return f"{base} reason={data.get('reason')!r}"
        if kind == "replan_throttled":
            return (
                f"{base} reason={data.get('reason')!r} trigger={data.get('trigger')!r}"
            )
        if kind == "budget_exhausted":
            return (
                f"{base} budget_name={data.get('budget_name')!r} "
                f"limit={data.get('limit')}"
            )
        if kind == "judgment_gated":
            return (
                f"{base} context={data.get('context')!r} "
                f"decision={data.get('decision')!r}"
            )
        if kind == "sensor_update":
            return f"{base} sensor={data.get('sensor')!r}"
        if kind == "sensor_error":
            return f"{base} sensor={data.get('sensor')!r} error={data.get('error')!r}"
        return base
