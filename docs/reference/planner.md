# Planner API Reference

## `goapauto.models.goap_planner.Planner`

The A* search engine used to find a plan.

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

### Methods

- **`generate_plan(world_state, goal, max_depth=None, heuristic_fn=None) -> PlanResult`**
  Synchronous plan generation. Returns a `PlanResult` with plan, message, schedule, and total_cost.

- **`async_generate_plan(world_state, goal, max_depth=None, heuristic_fn=None) -> PlanResult`**
  Asynchronous version.

- **`continue_plan(world_state, goal, executed_actions, max_depth=None, heuristic_fn=None) -> PlanResult`**
  Continue planning from a checkpoint after executing some actions. Skips already-executed actions.

- **`register_hook(event: str, callback: Callable)`**
  Register callbacks for events: `on_node_expanded`, `on_plan_found`, `on_search_failed`.

- **`get_search_graph() -> dict`**
  Returns the search graph from the last planning operation with nodes, edges, and metadata.

### Heuristics

- **`Node.heuristic(state, goal)`** - Default: counts unsatisfied conditions.
- **`Node.numeric_heuristic(state, goal)`** - Numeric-aware: uses absolute difference for numeric targets.

```python
planner = Planner(actions_list=actions, heuristic_fn=Node.numeric_heuristic)
```

### Types

- **`PlanResult`**: NamedTuple `(plan, message, schedule, makespan, total_cost)`.
- **`Schedule`**: Temporal schedule with steps (action, start_time, end_time, cost) and makespan.
- **`HeuristicFn`**: `Callable[[WorldState, Goal], float]`.
- **`Plan`**: `List[str]` of action names.
