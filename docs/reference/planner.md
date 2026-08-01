# Planner API Reference

The `Planner` class is the A\* search engine used to find a plan.

```python
from goapauto.models.goap_planner import Planner

planner = Planner(
    actions_list=[...],
    max_iterations=1000,
    heuristic_fn=custom_heuristic,
    verbose=True,          # print progress (default: True)
    logger=my_logger,      # custom logging.Logger
    cost_weights={         # weights for multi-dimensional costs
        "time": 1.0,
        "energy": 0.5
    }
)
result = planner.generate_plan(start_state, goal)
```

### Heuristics

```python
from goapauto.models.node import Node

planner = Planner(actions_list=actions, heuristic_fn=Node.numeric_heuristic)
```

::: goapauto.models.goap_planner.Planner
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

______________________________________________________________________

## PlanResult

The result of a planning operation, returned by `generate_plan`.

::: goapauto.models.goap_planner.PlanResult
    options:
        show_root_heading: true
        show_source: false
        inherited_members: false

______________________________________________________________________

## PlanStats

Statistics about the last planning process.

::: goapauto.models.goap_planner.PlanStats
    options:
        show_root_heading: true
        show_source: false
        inherited_members: false

______________________________________________________________________

## Schedule & ScheduleStep

Temporal planning types: a schedule of steps with start/end times and makespan.

::: goapauto.models.goap_planner.Schedule
    options:
        show_root_heading: true
        show_source: false
        inherited_members: false

::: goapauto.models.goap_planner.ScheduleStep
    options:
        show_root_heading: true
        show_source: false
        inherited_members: false
