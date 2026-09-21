# Replan API Reference

The `ReplanPolicy` class owns all replan bookkeeping: it detects when the current
plan or goal is stale and replans with time-budgeted search. It is the only
consumer-facing wrapper of the wave-1 runtime-budget seam
(`Planner(time_budget=...)`).

```python
from goapauto import ReplanPolicy, ReplanReason

policy = ReplanPolicy(
    planner,
    watched_keys=frozenset({"enemy_visible", "door_open"}),
    min_replan_interval=0.5,
    max_replans_per_tick=2,
)
policy.adopt(world_state, goal)

decision = policy.should_replan(world_state, goal, plan, tick_id=7)
if decision.replan:
    result = policy.replan(world_state, goal, decision, time_budget=0.1)
```

______________________________________________________________________

## ReplanPolicy

::: goapauto.models.replan.ReplanPolicy
    options:
        show_root_heading: true
        show_source: false
        inherited_members: false

______________________________________________________________________

## ReplanDecision

::: goapauto.models.replan.ReplanDecision
    options:
        show_root_heading: true
        show_source: false
        inherited_members: false

______________________________________________________________________

## ReplanReason

::: goapauto.models.replan.ReplanReason
    options:
        show_root_heading: true
        show_source: false
        inherited_members: false
