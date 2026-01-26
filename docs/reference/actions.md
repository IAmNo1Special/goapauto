# Actions API Reference

## `goapauto.models.actions.Action`

Represents an atomic action the agent can take.

```python
from goapauto.models.actions import Action, Increment

action = Action(
    name="chop_wood",
    preconditions={"has_axe": True},
    effects={"wood": Increment(1)},
    cost=1.0 # int or float (default: 1)
)
```

### Methods

- **`is_applicable(state: Any) -> bool`**
  Returns `True` if the state meets all `preconditions`.

- **`apply(state: Any) -> WorldState`**
  Returns a **new** `WorldState` with `effects` applied (immutable transition).

- **`async_apply(state: Any) -> WorldState`**
  Coroutine version of `apply` for async contexts.

---

## Predicates & Effects

### `goapauto.models.actions.Predicate`
Base class for conditions.
- **`Equal(value)`**: Checks equality.
- **`GreaterThan(value)`**: Checks `state_val > value`.
- **`LessThan(value)`**: Checks `state_val < value`.

### `goapauto.models.actions.Effect`
Base class for state mutations.
- **`Set(value)`**: Sets attribute to value.
- **`Increment(amount=1)`**: Adds amount.
- **`Decrement(amount=1)`**: Subtracts amount.
