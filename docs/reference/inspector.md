# Inspector API Reference

The `AgentInspector` is a passive "why this goal / plan / action?" diagnostics
recorder. It subscribes to the planner and replan-policy hooks, ingests
Jev/Judgment telemetry through explicit callbacks, and answers what the agent
decided, why, and what changed between ticks.

```python
from goapauto.utils.inspector import AgentInspector

inspector = AgentInspector()
inspector.attach(planner, arbitrator=arbitrator, sensor_manager=sensors)

for tick in loop:
    inspector.mark_tick(state)
    goal = arbitrator.select_goal(state)
    result = planner.generate_plan(state, goal)
    ...
    delta = inspector.what_changed_since(tick)

print(inspector.to_markdown())
inspector.to_jsonl("trace.jsonl")
inspector.detach()
```

The inspector never mutates the agent: hook callbacks are removed with the
same list objects on `detach()`, and every ingestion method is total — a
malformed record is captured as a diagnostic event, never raised.

::: goapauto.utils.inspector.AgentInspector
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

______________________________________________________________________

## Query Results

`why_goal()` and `why_plan()` return typed dataclasses; `what_changed_since()`
returns a `TickDelta`:

::: goapauto.utils.inspector.GoalDecision
    options:
        show_root_heading: true
        show_source: false

::: goapauto.utils.inspector.PlanDecision
    options:
        show_root_heading: true
        show_source: false

::: goapauto.utils.inspector.TickDelta
    options:
        show_root_heading: true
        show_source: false

## Trace Events

Every retained record is a frozen `TraceEvent` with a sequence number,
optional tick, monotonic timestamp, and JSON-safe data payload:

::: goapauto.utils.inspector.TraceEvent
    options:
        show_root_heading: true
        show_source: false

::: goapauto.utils.inspector.EventType
    options:
        show_root_heading: true
        show_source: false
