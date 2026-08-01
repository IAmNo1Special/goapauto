# Actions API Reference

## `goapauto.models.actions.Action`

Represents an atomic action the agent can take.

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

### Methods

- **`is_applicable(state: Any) -> bool`**
  Returns `True` if the state meets all `preconditions`.

- **`apply(state: Any) -> WorldState`**
  Returns a **new** `WorldState` with `effects` applied (immutable transition).

- **`async_apply(state: Any) -> WorldState`**
  Coroutine version of `apply` for async contexts.

- **`__post_init__()`**
  Validates cost, duration, and types on initialization.

______________________________________________________________________

## Predicates & Effects

### `goapauto.models.actions.Predicate`

Base class for conditions.

- **`Equal(value)`**: Checks equality.
- **`GreaterThan(value)`**: Checks `state_val > value`.
- **`LessThan(value)`**: Checks `state_val < value`.
- **`Range(min_value, max_value)`**: Checks `min <= state_val <= max` (inclusive).

### `goapauto.models.actions.Effect`

Base class for state mutations.

- **`Set(value)`**: Sets attribute to value.
- **`Increment(amount=1)`**: Adds amount.
- **`Decrement(amount=1)`**: Subtracts amount.
- **`Unset()`** / **`Delete()`**: Removes attribute from state.

### Positional Arguments

All predicates and effects support positional arguments:

```python
Set("kitchen")          # instead of Set(value="kitchen")
Increment(5)            # instead of Increment(amount=5)
Range(10, 20)           # Range(min=10, max=20)
```
