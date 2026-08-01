# Planner API Reference

The `Planner` class is the A\* search engine used to find and execute plans.

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

### Plan Execution & Handlers

The planner can execute generated plans step-by-step with custom execution handlers:

```python
# Register custom execution handler for an action
def handle_open_door(state, action):
    state.is_open = True
    return state

planner.register_execution_handler("open_door", handle_open_door)

# Execute plan synchronously or asynchronously
final_state = planner.execute_plan(initial_state, result)
# or: await planner.async_execute_plan(initial_state, result)
```

::: goapauto.models.goap_planner.Planner
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

______________________________________________________________________

## PlanExecutionError

Exception raised when plan execution fails (e.g., action precondition not met).

::: goapauto.models.goap_planner.PlanExecutionError
    options:
        show_root_heading: true
        show_source: false
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
