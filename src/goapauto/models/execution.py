"""Cooperative interruption of in-flight plan execution.

This module implements design 03: a :class:`PlanExecution` handle that lets
callers stop an in-flight run *between actions* -- never mid-action -- plus
the :class:`PlanInterruptedError` signal and the ``on_execution_interrupted``
hook event that together form the producer contract for why-diagnostics
(design 06).

The atomicity axiom: ``Action.apply`` / ``Action.async_apply`` and any
registered ``execution_handler`` are atomic units. An interrupt request is
therefore honored at the next action boundary -- after the currently-running
action (if any) completes and before the next action starts. The state at
that point is always a fully-applied, consistent ``WorldState``.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from typing import TYPE_CHECKING, Any, Literal, NoReturn, get_args

from goapauto.models.worldstate import WorldState

if TYPE_CHECKING:
    from goapauto.models.goal import Goal
    from goapauto.models.goap_planner import Planner

InterruptSource = Literal[
    "manual",
    "sensor",
    "watchdog",
    "budget",
    "arbitration",
    "confidence",
    "cancellation",
]
"""Who requested an interruption. Closed literal: diagnostics grouping keys on it."""

_ExecutionState = Literal[
    "initialized", "running", "completed", "interrupted", "failed"
]


def _check_interrupt_args(reason: str, source: str) -> None:
    """Validate ``interrupt()`` / error-constructor arguments, fail-loud."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("interrupt reason must be a non-empty string")
    if source not in get_args(InterruptSource):
        raise ValueError(
            f"interrupt source must be one of {get_args(InterruptSource)!r}, "
            f"got {source!r}"
        )


class PlanInterruptedError(ValueError):
    """Raised when a :class:`PlanExecution` run is interrupted at an action boundary.

    A sibling of ``PlanExecutionError`` under ``ValueError`` -- deliberately
    *not* a subclass. Interruption is not failure: existing
    ``except PlanExecutionError`` handlers (log, alert, retry, count) must not
    swallow deliberate interruptions. Code that wants "execution did not reach
    the goal" uniformly should catch
    ``(PlanInterruptedError, PlanExecutionError)``, with
    ``PlanInterruptedError`` first when the distinction matters.

    Never raised by ``execute_plan`` / ``async_execute_plan`` -- only by
    :meth:`PlanExecution.run` / :meth:`PlanExecution.arun`.
    """

    def __init__(
        self,
        message: str = "Plan execution was interrupted.",
        *,
        state: WorldState,
        executed_actions: list[str],
        remaining_actions: list[str],
        step_index: int,
        plan_length: int,
        state_diff: dict[str, tuple[Any, Any]],
        reason: str,
        source: InterruptSource,
    ) -> None:
        _check_interrupt_args(reason, source)
        super().__init__(message)
        # Defensive copies: the machinery passes an owned snapshot for
        # ``state``; the lists/dict are copied so later mutation by the
        # raiser cannot alias the payload.
        self.state = state
        self.executed_actions = list(executed_actions)
        self.remaining_actions = list(remaining_actions)
        self.step_index = step_index
        self.plan_length = plan_length
        self.state_diff = dict(state_diff)
        self.reason = reason
        self.source = source

    @property
    def next_action(self) -> str | None:
        """Name of the next action that never started, or ``None``.

        Convenience derivation from ``remaining_actions`` -- not carried data.
        """
        return self.remaining_actions[0] if self.remaining_actions else None


class PlanExecution:
    """Handle for one interruptible run of a plan.

    Created via :meth:`Planner.begin_execution <goapauto.models.goap_planner.Planner.begin_execution>`.
    The handle is single-shot: :meth:`run` / :meth:`arun` may each be called at
    most once -- a second call raises ``RuntimeError``.

    Thread-safety carve-out: the library is otherwise not thread-safe, but
    :meth:`interrupt` and the read-only inspection properties are safe to use
    from another thread (the watchdog pattern). ``run()`` / ``arun()`` must be
    called on exactly one thread.
    """

    def __init__(
        self,
        planner: Planner,
        initial_state: WorldState,
        plan_names: list[str],
        goal: Goal | None = None,
    ) -> None:
        self._planner = planner
        # The passed state object itself: the deep copy happens at run()/arun()
        # start (TOCTOU parity with execute_plan).
        self._initial_state = initial_state
        self._plan_names = list(plan_names)
        self._goal = goal
        self._lock = threading.Lock()
        self._state: _ExecutionState = "initialized"
        self._executed: list[str] = []
        self._interrupt_flag = False
        self._interrupt_reason = ""
        self._interrupt_source: InterruptSource = "manual"
        self._working_state: WorldState | None = None
        self._initial_snapshot: WorldState | None = None

    # ------------------------------------------------------------------
    # control
    # ------------------------------------------------------------------
    def interrupt(self, reason: str, *, source: InterruptSource = "manual") -> bool:
        """Request interruption of this execution.

        The request is honored at the next action boundary. First call wins:
        it records ``(reason, source)`` and returns ``True``; later calls
        return ``False`` and change nothing. Interrupting a terminal
        (completed/failed/interrupted) handle is a no-op returning ``False``.

        Argument validation runs before the terminal no-op check, so API
        misuse raises ``ValueError`` even on a finished handle.

        Safe to call from another thread.
        """
        _check_interrupt_args(reason, source)
        with self._lock:
            if self._state not in ("initialized", "running"):
                return False
            if self._interrupt_flag:
                return False
            self._interrupt_reason = reason
            self._interrupt_source = source
            self._interrupt_flag = True
            return True

    @property
    def is_interrupted(self) -> bool:
        """True iff the run stopped *because of* an interrupt. Terminal."""
        with self._lock:
            return self._state == "interrupted"

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------
    def run(self) -> WorldState:
        """Execute the plan synchronously to completion.

        Returns the final ``WorldState``. Raises :class:`PlanInterruptedError`
        when an interrupt is honored; ``PlanExecutionError`` / ``KeyError`` /
        hook exceptions propagate exactly as ``execute_plan`` does.
        Single-shot: a second call raises ``RuntimeError``.
        """
        with self._lock:
            if self._state != "initialized":
                raise RuntimeError(
                    "PlanExecution has already been run; handles are single-shot."
                )
            self._state = "running"
        try:
            result = self._run_sync()
        except PlanInterruptedError:
            raise
        except Exception:
            self._transition_terminal("failed")
            raise
        self._transition_terminal("completed")
        return result

    async def arun(self) -> WorldState:
        """Asynchronous twin of :meth:`run`.

        Honors async handlers. If the task running ``arun()`` is cancelled,
        the ``CancelledError`` propagates unchanged: the execution is marked
        interrupted (``source="cancellation"``) and
        ``on_execution_interrupted`` still fires, but a hook raising on that
        path is captured and logged rather than superseding the
        ``CancelledError``. Single-shot like :meth:`run`.
        """
        asyncio.get_running_loop()
        with self._lock:
            if self._state != "initialized":
                raise RuntimeError(
                    "PlanExecution has already been run; handles are single-shot."
                )
            self._state = "running"
        try:
            result = await self._arun_async()
        except PlanInterruptedError:
            raise
        except asyncio.CancelledError:
            self._note_cancellation()
            raise
        except Exception:
            self._transition_terminal("failed")
            raise
        self._transition_terminal("completed")
        return result

    # ------------------------------------------------------------------
    # inspection (immutable snapshots, safe cross-thread)
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._state == "running"

    @property
    def is_complete(self) -> bool:
        """True iff the run returned normally. Terminal."""
        with self._lock:
            return self._state == "completed"

    @property
    def executed_actions(self) -> tuple[str, ...]:
        """Action-name strings executed so far, in execution order."""
        with self._lock:
            return tuple(self._executed)

    @property
    def remaining_actions(self) -> tuple[str, ...]:
        """Action-name strings never started, in plan order."""
        with self._lock:
            return tuple(self._plan_names[len(self._executed) :])

    @property
    def state(self) -> WorldState:
        """Deep-copy snapshot of the current partial state."""
        with self._lock:
            if self._working_state is not None:
                return self._working_state.copy(deep=True)
            return self._initial_state.copy(deep=True)

    @property
    def interrupt_reason(self) -> str | None:
        """Reason of the first recorded interrupt request, if any."""
        with self._lock:
            return self._interrupt_reason if self._interrupt_flag else None

    @property
    def interrupt_source(self) -> InterruptSource | None:
        """Source of the first recorded interrupt request, if any."""
        with self._lock:
            return self._interrupt_source if self._interrupt_flag else None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _interrupt_requested(self) -> bool:
        with self._lock:
            return self._interrupt_flag

    def _transition_terminal(self, terminal: Literal["completed", "failed"]) -> None:
        # Only a running execution can reach a terminal state here: the
        # interrupt path transitions to "interrupted" itself before firing
        # its hook, so a hook raising there must not flip it to "failed".
        with self._lock:
            if self._state == "running":
                self._state = terminal

    def _take_interrupt_path(self) -> NoReturn:
        """Honor a pending interrupt: transition, snapshot, fire hook, raise."""
        with self._lock:
            self._state = "interrupted"
            reason = self._interrupt_reason
            source = self._interrupt_source
            executed = list(self._executed)
            remaining = list(self._plan_names[len(executed) :])
        # Invariant: the interrupt path only runs inside run()/arun(), after
        # the working snapshot was taken at start.
        assert self._working_state is not None
        assert self._initial_snapshot is not None
        state_copy = self._working_state.copy(deep=True)
        state_diff = self._initial_snapshot.diff(state_copy)
        # The hook gets its own fresh lists: a hook mutating its kwargs must
        # not corrupt the error payload raised after it (fire-then-raise).
        # Only ``state`` is shared with the error, per the producer contract.
        self._planner._trigger_hook(
            "on_execution_interrupted",
            reason=reason,
            source=source,
            state=state_copy,
            executed_actions=list(executed),
            remaining_actions=list(remaining),
            step_index=len(executed),
            plan_length=len(self._plan_names),
            state_diff=dict(state_diff),
            interrupted_at=time.time(),
        )
        raise PlanInterruptedError(
            state=state_copy,
            executed_actions=executed,
            remaining_actions=remaining,
            step_index=len(executed),
            plan_length=len(self._plan_names),
            state_diff=state_diff,
            reason=reason,
            source=source,
        )

    def _note_cancellation(self) -> None:
        """Mark the execution interrupted after ``arun()`` was cancelled.

        Fires ``on_execution_interrupted`` like the flag path, but hook errors
        are captured and logged: cancellation purity outranks hook fail-loud
        here, and the ``CancelledError`` must propagate unchanged.
        """
        with self._lock:
            if not self._interrupt_flag:
                self._interrupt_reason = "task cancelled"
                self._interrupt_source = "cancellation"
                self._interrupt_flag = True
            reason = self._interrupt_reason
            source = self._interrupt_source
            executed = list(self._executed)
            remaining = list(self._plan_names[len(executed) :])
            if self._state == "running":
                self._state = "interrupted"
        assert self._working_state is not None
        assert self._initial_snapshot is not None
        state_copy = self._working_state.copy(deep=True)
        state_diff = self._initial_snapshot.diff(state_copy)
        try:
            self._planner._trigger_hook(
                "on_execution_interrupted",
                reason=reason,
                source=source,
                state=state_copy,
                executed_actions=executed,
                remaining_actions=remaining,
                step_index=len(executed),
                plan_length=len(self._plan_names),
                state_diff=state_diff,
                interrupted_at=time.time(),
            )
        except Exception:
            self._planner._logger.exception(
                "on_execution_interrupted hook raised during task cancellation; "
                "the CancelledError still propagates unchanged"
            )

    def _run_sync(self) -> WorldState:
        from goapauto.models.goap_planner import PlanExecutionError

        planner = self._planner
        initial_snapshot = self._initial_state.copy(deep=True)
        self._initial_snapshot = initial_snapshot
        working_state = initial_snapshot.copy(deep=True)
        self._working_state = working_state

        # Pre-loop boundary: covers interrupt-before-first-action and the
        # empty plan.
        if self._interrupt_requested():
            self._take_interrupt_path()

        for name in self._plan_names:
            action = planner._get_action_by_name(name, working_state, self._goal)
            if action is None:
                raise KeyError(f"Action '{name}' not found in registered providers.")
            # Interrupt check comes before the applicability check: a
            # deliberate control signal wins; precondition fallout surfaces
            # on the subsequent replan from the preserved partial state.
            if self._interrupt_requested():
                self._take_interrupt_path()
            if not action.is_applicable(working_state):
                planner._trigger_hook(
                    "on_action_failed", action=action, state=working_state
                )
                planner._trigger_hook(
                    "on_execution_failed", action=action, state=working_state
                )
                raise PlanExecutionError(
                    f"Action '{action.name}' is not applicable to current state."
                )
            planner._trigger_hook("on_action_start", action=action, state=working_state)
            try:
                if name in planner.execution_handlers:
                    handler = planner.execution_handlers[name]
                    if inspect.iscoroutinefunction(handler):
                        raise TypeError(
                            f"Async execution handler for action '{name}' cannot be "
                            "used in synchronous run. Use arun instead."
                        )
                    working_state = handler(working_state, action)
                else:
                    working_state = action.apply(working_state)
                self._working_state = working_state
            except Exception as e:
                if not isinstance(e, (TypeError, PlanExecutionError)):
                    planner._trigger_hook(
                        "on_action_failed", action=action, state=working_state
                    )
                    planner._trigger_hook(
                        "on_execution_failed", action=action, state=working_state
                    )
                raise
            with self._lock:
                self._executed.append(name)
            planner._trigger_hook(
                "on_action_complete", action=action, state=working_state
            )

        # Post-loop boundary: honors an interrupt requested from the last
        # action's on_action_complete instead of completing silently.
        if self._interrupt_requested():
            self._take_interrupt_path()

        planner._trigger_hook("on_execution_complete", state=working_state)
        return working_state

    async def _arun_async(self) -> WorldState:
        from goapauto.models.goap_planner import PlanExecutionError

        planner = self._planner
        initial_snapshot = self._initial_state.copy(deep=True)
        self._initial_snapshot = initial_snapshot
        working_state = initial_snapshot.copy(deep=True)
        self._working_state = working_state

        if self._interrupt_requested():
            self._take_interrupt_path()

        for name in self._plan_names:
            action = planner._get_action_by_name(name, working_state, self._goal)
            if action is None:
                raise KeyError(f"Action '{name}' not found in registered providers.")
            if self._interrupt_requested():
                self._take_interrupt_path()
            if not action.is_applicable(working_state):
                planner._trigger_hook(
                    "on_action_failed", action=action, state=working_state
                )
                planner._trigger_hook(
                    "on_execution_failed", action=action, state=working_state
                )
                raise PlanExecutionError(
                    f"Action '{action.name}' is not applicable to current state."
                )
            planner._trigger_hook("on_action_start", action=action, state=working_state)
            try:
                if name in planner.execution_handlers:
                    handler = planner.execution_handlers[name]
                    if inspect.iscoroutinefunction(handler):
                        working_state = await handler(working_state, action)
                    else:
                        working_state = handler(working_state, action)
                else:
                    working_state = await action.async_apply(working_state)
                self._working_state = working_state
            except Exception as e:
                if not isinstance(e, PlanExecutionError):
                    planner._trigger_hook(
                        "on_action_failed", action=action, state=working_state
                    )
                    planner._trigger_hook(
                        "on_execution_failed", action=action, state=working_state
                    )
                raise
            with self._lock:
                self._executed.append(name)
            planner._trigger_hook(
                "on_action_complete", action=action, state=working_state
            )

        if self._interrupt_requested():
            self._take_interrupt_path()

        planner._trigger_hook("on_execution_complete", state=working_state)
        return working_state
