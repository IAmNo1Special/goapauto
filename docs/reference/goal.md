# Goal API Reference

The `Goal` class represents a target state to achieve.

```python
from goapauto.models.goal import Goal
from goapauto.models.actions import GreaterThan, LessThan, Range

goal = Goal(
    target_state={
        "health": GreaterThan(50),
        "ammo": Range(10, 100),
        "enemy_dead": True
    },
    priority=1, # Lower number = higher priority (must be >= 1)
    name="Survive"
)
```

### Callable Targets

Goal targets can be callables (lambdas, predicates) for flexible conditions:

```python
goal = Goal(
    target_state={
        "health": lambda v: v >= 50,      # custom predicate
        "ammo": lambda v: 10 <= v <= 100, # inline lambda
        "alive": True
    }
)
```

### Range & Inequality Goals

Use built-in predicates for common numeric constraints:

```python
# At least 10
{"health": GreaterThan(10)}

# At most 50
{"weight": LessThan(50)}

# Range (inclusive)
{"ammo": Range(10, 100)}
```

::: goapauto.models.goal.Goal
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false
