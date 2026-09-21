# Execution Interruption API Reference

Cooperative interruption of in-flight plan execution. An interrupt request is
honored at the next action boundary -- after the currently-running action (if
any) completes and before the next action starts. Actions are atomic: there is
no mid-action checkpoint and no preemption of a running handler.

```python
from goapauto import Planner, PlanInterruptedError

planner = Planner(actions_list=[...])
execution = planner.begin_execution(state, plan, goal)

# From anywhere with a reference to the handle -- another thread, a hook,
# a watchdog, the Soulscape tick loop:
execution.interrupt("enemy_sighted", source="sensor")

try:
    final = execution.run()
except PlanInterruptedError as err:
    print(err.reason)            # "enemy_sighted"
    print(err.executed_actions)  # ["move_to_cover", ...]
    print(err.next_action)       # action that never started
    # Replan from the exact partial-state snapshot:
    new_plan = planner.continue_plan(err.state, goal, list(err.executed_actions))
```

The sensor-staleness pull wiring is an `on_action_complete` closure over the
sensor's generation counter that calls `execution.interrupt(...)` when the
world moved. There is deliberately no predicate seam: the handle covers
pre-run (flag), per-boundary pull (hook closure), and async watchdogs
(threads).

______________________________________________________________________

## PlanExecution

The execution handle, created via `Planner.begin_execution`. Single-shot:
`run()` / `arun()` raise `RuntimeError` on a second call. `interrupt()` and
the read-only inspection properties are safe to use from another thread;
`run()` / `arun()` must be called on exactly one thread.

::: goapauto.models.execution.PlanExecution
    options:
        show_root_heading: true
        show_source: false
        inherited_members: false

______________________________________________________________________

## PlanInterruptedError

Raised when a run is interrupted. A `ValueError` sibling of
`PlanExecutionError` -- deliberately not a subclass, so existing
`except PlanExecutionError` failure handlers never swallow a deliberate
interruption. Never raised by `execute_plan` / `async_execute_plan`.

::: goapauto.models.execution.PlanInterruptedError
    options:
        show_root_heading: true
        show_source: false
        inherited_members: false

______________________________________________________________________

## InterruptSource

Closed literal naming *who* interrupted: `"manual"`, `"sensor"`,
`"watchdog"`, `"budget"`, `"arbitration"`, `"confidence"`,
`"cancellation"`. Diagnostics grouping keys on it; the free-form `reason`
carries the human detail.

::: goapauto.models.execution.InterruptSource
    options:
        show_root_heading: true
        show_source: false
        inherited_members: false

______________________________________________________________________

## on_execution_interrupted

Hook event fired exactly once per interrupted run -- on every interruption
path, including the `asyncio.CancelledError` path -- and then the machinery
raises. Never fired alongside `on_execution_complete` / `on_execution_failed`
for the same run. Exact kwargs: `reason`, `source`, `state`,
`executed_actions`, `remaining_actions`, `step_index`, `plan_length`,
`state_diff`, `interrupted_at`.

```python
planner.register_hook(
    "on_execution_interrupted",
    lambda *, reason, source, state, executed_actions, remaining_actions,
             step_index, plan_length, state_diff, interrupted_at: ...,
)
```
