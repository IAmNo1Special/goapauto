# Goal API Reference

## `goapauto.models.goal.Goal`

Represents a target state to achieve.

```python
from goapauto.models.goal import Goal

goal = Goal(
    target_state={"has_wood": True},
    priority=1, # Lower number = higher priority (must be >= 1)
    name="Collect Wood"
)
```

### Methods

- **`is_satisfied(world_state: Any) -> bool`**
  Checks if all conditions in `target_state` are met by `world_state`.

- **`get_unsatisfied_conditions(world_state: Any) -> Dict`**
  Returns conditions that are not yet met in the current state.
