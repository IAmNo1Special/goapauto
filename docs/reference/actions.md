# Actions API Reference

The `Action` class represents an atomic action the agent can take.

```python
from goapauto.models.actions import Action, Increment

action = Action(
    name="chop_wood",
    preconditions={"has_axe": True},
    effects={"wood": Increment(1)},
    cost=1.0,           # int or float (default: 1)
    duration=2.0        # optional duration for temporal planning
)
```

### Multi-dimensional Costs

Action costs can be scalar (float) or multi-dimensional (dict/list) for complex optimization:

```python
# Dictionary costs with named dimensions
action = Action(
    name="navigate",
    preconditions={},
    effects={"location": Set("target")},
    cost={"time": 30.0, "energy": 5.0, "risk": 2.0}
)

# List costs with positional dimensions
action = Action(
    name="build",
    preconditions={},
    effects={"structure": True},
    cost=[10.0, 5.0, 2.0]  # [time, energy, cost]
)
```

Use with `Planner(cost_weights=...)` to optimize weighted combinations.

### Duration for Temporal Planning

```python
action = Action(
    name="build",
    preconditions={},
    effects={"structure": True},
    cost=10.0,
    duration=30.0  # seconds
)
```

::: goapauto.models.actions.Action
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

______________________________________________________________________

## Action Collections

::: goapauto.models.actions.Actions
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

______________________________________________________________________

## Predicates & Effects

Predicates are used in preconditions to check state values; effects define how state changes when an action is applied.

### Predicates

All predicates support positional arguments:

```python
Range(10, 20)           # Range(min=10, max=20)
GreaterThan(5)
LessThan(50)
```

::: goapauto.models.actions.Predicate
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false
        members: [__call__]

::: goapauto.models.actions.Equal
    options:
        show_root_heading: true
        show_source: false
        show_bases: false

::: goapauto.models.actions.NotEqual
    options:
        show_root_heading: true
        show_source: false
        show_bases: false

::: goapauto.models.actions.GreaterThan
    options:
        show_root_heading: true
        show_source: false
        show_bases: false

::: goapauto.models.actions.LessThan
    options:
        show_root_heading: true
        show_source: false
        show_bases: false

::: goapauto.models.actions.Range
    options:
        show_root_heading: true
        show_source: false
        show_bases: false

### Effects

All effects support positional arguments:

```python
Set("kitchen")          # instead of Set(value="kitchen")
Increment(5)            # instead of Increment(amount=5)
Decrement(3)
Unset()                 # remove attribute
Delete()                # alias for Unset
```

::: goapauto.models.actions.Effect
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false
        members: [__call__]

::: goapauto.models.actions.Set
    options:
        show_root_heading: true
        show_source: false
        show_bases: false

::: goapauto.models.actions.Increment
    options:
        show_root_heading: true
        show_source: false
        show_bases: false

::: goapauto.models.actions.Decrement
    options:
        show_root_heading: true
        show_source: false
        show_bases: false

::: goapauto.models.actions.Unset
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
