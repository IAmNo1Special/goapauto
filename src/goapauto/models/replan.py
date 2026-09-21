"""Replan throttling policy: when to replan, and through which planner call.

Design 01 (runtime & replan budgets). The policy is fully opt-in: construct it
around an existing :class:`~goapauto.models.goap_planner.Planner`, adopt the
plan you hold with :meth:`ReplanPolicy.adopt`, then ask
:meth:`ReplanPolicy.should_replan` between actions and
:meth:`ReplanPolicy.replan` through the resulting decision.

The policy never executes anything and never touches provider or sensor
types: staleness arrives as a plain ``sensor_age`` float, interruption as a
``was_interrupted`` flag. The host owns those translations.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, cast, overload

from goapauto.models.actions import Action
from goapauto.models.goal import Goal
from goapauto.models.goap_planner import (
    Plan,
    Planner,
    PlanResult,
    _validate_time_budget,
)
from goapauto.models.worldstate import WorldState


class ReplanReason(str, Enum):  # noqa: UP042 - design 01 §2.4 binds (str, Enum)
    """Why a replan decision came out the way it did.

    A ``str`` enum: the wire form is the plain string value, which is also
    what the diagnostics layer's string-typed fields consume
    (``decision.reason.value``).
    """

    NO_PLAN = "no_plan"
    GOAL_SATISFIED = "goal_satisfied"
    PLAN_VALID = "plan_valid"
    PLAN_INVALID = "plan_invalid"
    GOAL_CHANGED = "goal_changed"
    WORLD_CHANGED = "world_changed"
    THROTTLED = "throttled"
    SENSORS_STALE = "sensors_stale"


@dataclass(frozen=True)
class ReplanDecision:
    """Outcome of :meth:`ReplanPolicy.should_replan`.

    Frozen for value semantics but not hashable (``detail`` is a dict):
    equality works, ``hash()`` does not. ``detail`` values are restricted
    to JSON-safe scalars (``str``, ``int``, ``float``, ``bool``, ``None``,
    and lists thereof) -- the diagnostics layer serializes decisions, so
    the offending action is recorded as its name string, never the
    :class:`Action` object.
    """

    replan: bool
    reason: ReplanReason
    detail: dict[str, Any] = field(default_factory=dict)


@overload
def _validate_nonnegative_number(
    name: str, value: float, *, allow_none: Literal[False]
) -> float: ...


@overload
def _validate_nonnegative_number(
    name: str, value: float | None, *, allow_none: Literal[True]
) -> float | None: ...


def _validate_nonnegative_number(
    name: str, value: float | None, *, allow_none: bool
) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}")
    if math.isnan(value) or value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


def _validate_positive_int(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")
    return value


def _validate_bool(name: str, value: bool) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool, got {type(value).__name__}")
    return value


class ReplanPolicy:
    """Decides when to replan and replans through the wrapped planner.

    The host drives the loop: ``adopt()`` once (or let ``replan()`` adopt
    implicitly), ``should_replan()`` between actions, ``replan()`` on a
    replan decision, ``note_executed()`` after each executed action.

    Everything is opt-in; the defaults only install the two structural
    triggers (invalid next action, changed goal). Not thread-safe, like
    the planner it wraps.
    """

    _UNTHROTTLED_TRIGGERS = frozenset(
        {
            ReplanReason.NO_PLAN,
            ReplanReason.PLAN_INVALID,
            ReplanReason.GOAL_CHANGED,
        }
    )

    def __init__(
        self,
        planner: Planner,
        min_replan_interval: float = 0.0,
        replan_on_invalid: bool = True,
        replan_on_goal_change: bool = True,
        watched_keys: frozenset[str] | None = None,
        max_sensor_age: float | None = None,
        time_budget: float | None = None,
        max_replans_per_tick: int | None = None,
        max_consecutive_unthrottled_replans: int | None = None,
        reuse_prefix_on_invalid: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a policy around an existing planner.

        Args:
            planner: The planner replans are executed through.
            min_replan_interval: Seconds between optional (world-change)
                replans; ``0.0`` disables throttling.
            replan_on_invalid: Replan when the next action's preconditions
                fail (bypasses the interval: a broken plan cannot execute).
            replan_on_goal_change: Replan when the selected goal differs
                from the planned-for goal (also bypasses the interval).
            watched_keys: World-state keys whose change triggers a replan;
                ``None`` disables the world-change trigger.
            max_sensor_age: When set, a world-change replan is suppressed
                while ``sensor_age`` exceeds it (``SENSORS_STALE``);
                structural triggers still fire.
            time_budget: Passed through to the planner calls ``replan()``
                makes; validated eagerly, exactly like the planner's own.
            max_replans_per_tick: Within-tick replan cap; requires the host
                to pass ``tick_id`` to ``should_replan``. ``None`` disables.
            max_consecutive_unthrottled_replans: Circuit breaker -- after
                this many consecutive unthrottled replans the circuit opens
                and further unthrottled triggers are answered ``THROTTLED``
                until steady state, ``adopt()``, ``reset()``, or a
                successful world-change probe replan closes it.
                ``None`` disables.
            reuse_prefix_on_invalid: Route invalid/world-changed replans
                through ``continue_plan`` (prefix reuse); ``False`` routes
                them through a fresh ``generate_plan`` (for cyclic/toggle
                domains where order-based prefix stripping can drop a
                needed step).
            clock: Injectable clock (seconds); ``time.monotonic`` default.
        """
        self._planner = planner
        self._min_replan_interval = _validate_nonnegative_number(
            "min_replan_interval", min_replan_interval, allow_none=False
        )
        self._replan_on_invalid = _validate_bool("replan_on_invalid", replan_on_invalid)
        self._replan_on_goal_change = _validate_bool(
            "replan_on_goal_change", replan_on_goal_change
        )
        self._watched_keys = (
            frozenset(watched_keys) if watched_keys is not None else None
        )
        self._max_sensor_age = _validate_nonnegative_number(
            "max_sensor_age", max_sensor_age, allow_none=True
        )
        _validate_time_budget(time_budget)
        self._time_budget = time_budget
        self._max_replans_per_tick = _validate_positive_int(
            "max_replans_per_tick", max_replans_per_tick
        )
        self._max_consecutive_unthrottled_replans = _validate_positive_int(
            "max_consecutive_unthrottled_replans", max_consecutive_unthrottled_replans
        )
        self._reuse_prefix_on_invalid = _validate_bool(
            "reuse_prefix_on_invalid", reuse_prefix_on_invalid
        )
        self._clock = clock

        self.hooks: dict[str, list[Callable[..., Any]]] = {
            "on_replan": [],
            "on_replan_skipped": [],
        }
        self._reset_bookkeeping()

    def _reset_bookkeeping(self) -> None:
        self._adopted = False
        self._state_snapshot: WorldState | None = None
        self._last_goal: Goal | None = None
        self._last_plan_time = 0.0
        self._executed: list[str] = []
        self._pending_state: WorldState | None = None
        self._pending_goal: Goal | None = None
        self._replans_this_tick = 0
        self._last_tick_id: int | str | None = None
        self._last_replan_tick_id: int | str | None = None
        self._unthrottled_streak = 0
        self._circuit_open = False

    def register_hook(self, event: str, callback: Callable[..., Any]) -> None:
        """Register a callback for a policy event.

        Args:
            event: One of 'on_replan', 'on_replan_skipped'
            callback: The function to call when the event occurs

        Raises:
            ValueError: If the event name is unknown.
        """
        if event in self.hooks:
            self.hooks[event].append(callback)
        else:
            raise ValueError(f"Unknown event hook: {event}")

    def _trigger_hook(self, event: str, **kwargs: Any) -> None:
        for callback in self.hooks[event]:
            callback(**kwargs)

    def adopt(self, state: WorldState, goal: Goal) -> None:
        """Adopt an externally-created plan as the policy's baseline.

        Seeds the state snapshot, the planned-for goal, and the plan clock;
        clears the executed prefix and resets the streak/circuit/tick
        counters. Required before the first :meth:`should_replan` --
        without it the policy would diff against nothing and hallucinate a
        goal change. ``replan()`` also adopts implicitly after planning.
        """
        if not isinstance(state, WorldState):
            raise TypeError(f"state must be a WorldState, got {type(state).__name__}")
        if not isinstance(goal, Goal):
            raise TypeError(f"goal must be a Goal, got {type(goal).__name__}")
        self._state_snapshot = state.model_copy(deep=True)
        self._last_goal = goal.model_copy(deep=True)
        self._last_plan_time = self._clock()
        self._executed = []
        self._pending_state = None
        self._pending_goal = None
        self._replans_this_tick = 0
        self._last_tick_id = None
        self._last_replan_tick_id = None
        self._unthrottled_streak = 0
        self._circuit_open = False
        self._adopted = True

    def reset(self) -> None:
        """Forget everything, including adoption.

        After ``reset()`` the policy must be adopted again before
        :meth:`should_replan` will answer.
        """
        self._reset_bookkeeping()

    def note_executed(self, action_name: str) -> None:
        """Record an executed action name for prefix reuse on replan.

        The policy never executes anything itself; the host calls this
        after each executed action (or bridges design 03's
        ``PlanInterruptedError.executed_actions`` into it).
        """
        if not isinstance(action_name, str):
            raise TypeError(
                f"action_name must be a str, got {type(action_name).__name__}"
            )
        self._executed.append(action_name)

    def should_replan(
        self,
        state: WorldState,
        goal: Goal,
        plan: Plan | PlanResult | None,
        next_index: int = 0,
        *,
        sensor_age: float | None = None,
        was_interrupted: bool = False,
        tick_id: int | str | None = None,
        emit_hooks: bool = True,
    ) -> ReplanDecision:
        """Decide whether to replan, in a fixed evaluation order.

        Order: goal satisfied -> no plan -> next-action validity (or forced
        by ``was_interrupted``) -> goal changed -> staleness gate ->
        world change (throttled) -> plan valid. A would-be replan is then
        suppressed by the circuit breaker, then the per-tick cap.

        Args:
            state: Current world state.
            goal: Currently selected goal.
            plan: The plan under execution -- a ``Plan`` list, the
                ``PlanResult`` it came from (unwrapped), or ``None``.
            next_index: Index of the next action to execute.
            sensor_age: Seconds since last good sensor data; ``None`` means
                unknown/fresh. Only consulted with the world-change trigger
                when ``max_sensor_age`` is set.
            was_interrupted: Design-03 interruption landed here -- forces
                ``PLAN_INVALID`` regardless of applicability.
            tick_id: Opaque tick context, passed through into
                ``detail`` and hooks; required when ``max_replans_per_tick``
                is set.
            emit_hooks: ``False`` decides without firing
                ``on_replan_skipped`` (side-effect-free preview).

        Returns:
            A frozen :class:`ReplanDecision`.

        Raises:
            ValueError: If the policy has no adopted plan, ``next_index``
                is negative, or a per-tick cap is set without ``tick_id``.
            TypeError: If ``state``/``goal``/``plan`` have invalid types.
        """
        if not self._adopted:
            raise ValueError(
                "policy has no adopted plan: call adopt() or replan() first"
            )
        if not isinstance(state, WorldState):
            raise TypeError(f"state must be a WorldState, got {type(state).__name__}")
        if not isinstance(goal, Goal):
            raise TypeError(f"goal must be a Goal, got {type(goal).__name__}")
        if isinstance(plan, PlanResult):
            plan = plan.plan
        elif plan is not None and not isinstance(plan, list):
            raise TypeError(
                f"plan must be a Plan, PlanResult, or None, got {type(plan).__name__}"
            )
        if next_index < 0:
            raise ValueError(f"next_index must be >= 0, got {next_index}")
        if self._max_replans_per_tick is not None and tick_id is None:
            raise ValueError("max_replans_per_tick is set but no tick_id was passed")
        if tick_id is not None and tick_id != self._last_tick_id:
            self._replans_this_tick = 0
            self._last_tick_id = tick_id

        detail_base: dict[str, Any] = {}
        if tick_id is not None:
            detail_base["tick_id"] = tick_id

        # 1. Goal satisfied: nothing to do.
        if goal.is_satisfied(state):
            return self._record_decision(
                ReplanDecision(False, ReplanReason.GOAL_SATISFIED, dict(detail_base)),
                state,
                goal,
                emit_hooks,
            )

        trigger: ReplanReason | None = None
        detail: dict[str, Any] = dict(detail_base)

        # 2. No plan (or the plan is spent): the interval never blocks the
        # first plan, but the circuit breaker still applies below.
        if plan is None or next_index >= len(plan):
            trigger = ReplanReason.NO_PLAN
        else:
            # 3. Next-action validity, or forced by interruption. An
            # interrupted-but-unstarted action is usually still applicable,
            # so the interruption is a control signal, not a state fact.
            step = plan[next_index]
            name = step.name if isinstance(step, Action) else step
            if not isinstance(name, str):
                raise TypeError(
                    "plan steps must be action name strings or Action objects, "
                    f"got {type(step).__name__}"
                )
            if self._replan_on_invalid or was_interrupted:
                action: Action | None = None
                if not was_interrupted:
                    action = self._planner.get_action(name, state, goal=goal)
                if was_interrupted or action is None or not action.is_applicable(state):
                    trigger = ReplanReason.PLAN_INVALID
                    detail = {
                        "action": name,
                        "interrupted": bool(was_interrupted),
                        **detail_base,
                    }

        # 4. Goal change: the old plan was built for a different objective.
        if trigger is None and self._replan_on_goal_change and goal != self._last_goal:
            trigger = ReplanReason.GOAL_CHANGED

        # 5. Staleness gate: evaluated before the interval throttle, so
        # garbage sensor data reports the actionable signal, not "too soon".
        stale = (
            self._max_sensor_age is not None
            and sensor_age is not None
            and sensor_age > self._max_sensor_age
        )

        # 6. World change on watched keys.
        if trigger is None and self._watched_keys is not None:
            snapshot = cast(WorldState, self._state_snapshot)
            changed = set(state.diff(snapshot)) & self._watched_keys
            if changed:
                if stale:
                    return self._record_decision(
                        ReplanDecision(
                            False,
                            ReplanReason.SENSORS_STALE,
                            {"sensor_age": sensor_age, **detail_base},
                        ),
                        state,
                        goal,
                        emit_hooks,
                    )
                elapsed = self._clock() - self._last_plan_time
                if elapsed < self._min_replan_interval:
                    return self._record_decision(
                        ReplanDecision(
                            False,
                            ReplanReason.THROTTLED,
                            {
                                "throttled_by": "min_replan_interval",
                                "changed_keys": sorted(changed),
                                "elapsed": elapsed,
                                "cooldown_remaining_ms": (
                                    self._min_replan_interval - elapsed
                                )
                                * 1000.0,
                                **detail_base,
                            },
                        ),
                        state,
                        goal,
                        emit_hooks,
                    )
                trigger = ReplanReason.WORLD_CHANGED
                detail = {"changed_keys": sorted(changed), **detail_base}

        # 7. Otherwise the plan is still good.
        if trigger is None:
            return self._record_decision(
                ReplanDecision(False, ReplanReason.PLAN_VALID, dict(detail_base)),
                state,
                goal,
                emit_hooks,
            )

        # Suppression order: circuit breaker, then per-tick cap. The
        # interval was already handled at step 6 for WORLD_CHANGED; the
        # structural triggers bypass it by design.
        if trigger in self._UNTHROTTLED_TRIGGERS and self._circuit_open:
            return self._record_decision(
                ReplanDecision(
                    False,
                    ReplanReason.THROTTLED,
                    {
                        "throttled_by": "circuit_breaker",
                        "consecutive_unthrottled_replans": self._unthrottled_streak,
                        "last_replan_tick_id": self._last_replan_tick_id,
                        **detail_base,
                    },
                ),
                state,
                goal,
                emit_hooks,
            )
        if (
            self._max_replans_per_tick is not None
            and self._replans_this_tick >= self._max_replans_per_tick
        ):
            return self._record_decision(
                ReplanDecision(
                    False,
                    ReplanReason.THROTTLED,
                    {
                        "throttled_by": "max_replans_per_tick",
                        "replans_this_tick": self._replans_this_tick,
                        "max_replans_per_tick": self._max_replans_per_tick,
                        **detail_base,
                    },
                ),
                state,
                goal,
                emit_hooks,
            )
        return self._record_decision(
            ReplanDecision(True, trigger, detail), state, goal, emit_hooks
        )

    def _record_decision(
        self,
        decision: ReplanDecision,
        state: WorldState,
        goal: Goal,
        emit_hooks: bool,
    ) -> ReplanDecision:
        # Steady state closes the circuit: the breaker measures consecutive
        # ticks without a usable plan, not mere replan count.
        if decision.reason in (ReplanReason.PLAN_VALID, ReplanReason.GOAL_SATISFIED):
            self._unthrottled_streak = 0
            self._circuit_open = False
        self._pending_state = state
        self._pending_goal = goal
        if (
            emit_hooks
            and not decision.replan
            and decision.reason in (ReplanReason.THROTTLED, ReplanReason.SENSORS_STALE)
        ):
            self._trigger_hook("on_replan_skipped", decision=decision)
        return decision

    def replan(
        self,
        state: WorldState,
        goal: Goal,
        decision: ReplanDecision,
        tick_id: int | str | None = None,
    ) -> PlanResult:
        """Act on a replan decision through the wrapped planner.

        Re-checks nothing about the world; it validates the call, plans,
        and adopts the outcome implicitly. ``PLAN_INVALID`` /
        ``WORLD_CHANGED`` route through ``continue_plan`` (reusing the
        executed prefix) unless ``reuse_prefix_on_invalid`` is ``False``;
        ``GOAL_CHANGED`` / ``NO_PLAN`` take a fresh ``generate_plan``.

        Args:
            state: The state the decision was computed from (identity
                checked -- pass the same objects).
            goal: The goal the decision was computed from (identity checked).
            decision: A decision with ``replan=True`` from
                :meth:`should_replan`.
            tick_id: Opaque tick context; defaults to the decision's
                ``tick_id`` when present.

        Returns:
            The fresh ``PlanResult``.

        Raises:
            ValueError: If the decision says not to replan, carries an
                unexpected reason, or was computed from different
                state/goal objects.
        """
        if not decision.replan:
            raise ValueError("cannot replan on a decision with replan=False")
        if decision.reason not in (
            ReplanReason.NO_PLAN,
            ReplanReason.PLAN_INVALID,
            ReplanReason.GOAL_CHANGED,
            ReplanReason.WORLD_CHANGED,
        ):
            raise ValueError(
                f"cannot replan on reason {decision.reason.value!r}: "
                "not a replan trigger"
            )
        if state is not self._pending_state or goal is not self._pending_goal:
            raise ValueError(
                "replan() must receive the state and goal objects the "
                "decision was computed from"
            )

        effective_tick = (
            tick_id if tick_id is not None else decision.detail.get("tick_id")
        )
        if effective_tick is not None:
            if effective_tick != self._last_tick_id:
                self._replans_this_tick = 0
                self._last_tick_id = effective_tick
            self._replans_this_tick += 1
        self._last_replan_tick_id = effective_tick

        if (
            decision.reason in (ReplanReason.PLAN_INVALID, ReplanReason.WORLD_CHANGED)
            and self._reuse_prefix_on_invalid
        ):
            result = self._planner.continue_plan(
                state,
                goal,
                executed_actions=list(self._executed),
                time_budget=self._time_budget,
            )
        else:
            result = self._planner.generate_plan(
                state, goal, time_budget=self._time_budget
            )

        self._last_plan_time = self._clock()
        self._state_snapshot = state.model_copy(deep=True)
        self._last_goal = goal.model_copy(deep=True)
        self._executed = []
        self._adopted = True

        if decision.reason in self._UNTHROTTLED_TRIGGERS:
            self._unthrottled_streak += 1
            if (
                self._max_consecutive_unthrottled_replans is not None
                and self._unthrottled_streak
                >= self._max_consecutive_unthrottled_replans
            ):
                self._circuit_open = True
        elif decision.reason == ReplanReason.WORLD_CHANGED:
            # A successful probe replan closes the circuit: the probe found
            # a usable plan, breaking the "no usable plan" streak. A failed
            # probe leaves the breaker as it was.
            if result.plan is not None:
                self._unthrottled_streak = 0
                self._circuit_open = False

        self._trigger_hook("on_replan", decision=decision, result=result)
        return result
