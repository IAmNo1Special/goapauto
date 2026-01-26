# Planner API Reference

## `goapauto.models.goap_planner.Planner`

The A* search engine used to find a plan.

```python
from goapauto.models.goap_planner import Planner

planner = Planner(actions_list=[...])
result = planner.generate_plan(start_state, goal)
```

### Methods

- **`generate_plan(world_state, goal, max_depth=None, heuristic_fn=None) -> PlanResult`**
  Synchronous plan generation. Returns a `PlanResult`.

- **`async_generate_plan(world_state, goal, max_depth=None, heuristic_fn=None) -> PlanResult`**
  Asynchronous version.

- **`register_hook(event: str, callback: Callable)`**
  Register callbacks for events: `on_node_expanded`, `on_plan_found`, `on_search_failed`.

### Types

- **`PlanResult`**: NamedTuple `(plan: Optional[List[str]], message: str)`.
- **`HeuristicFn`**: `Callable[[WorldState, Goal], float]`.
