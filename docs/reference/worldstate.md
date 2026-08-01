# WorldState API Reference

## `goapauto.models.worldstate.WorldState`

A class representing the world state with attribute-style access. Inherits from `pydantic.BaseModel`.

```python
from goapauto.models.worldstate import WorldState

# Initialization (Keyword-only arguments)
state = WorldState(has_wood=True, count=5)
# dictionary access
val = state['has_wood']
# attribute access
val = state.has_wood
```

### Distinguishing Unknown from False/None

`WorldState` distinguishes between "never set" and "explicitly set to False/None":

```python
from goapauto.models.worldstate import _UNKNOWN

state = WorldState()
state.get("x")                    # returns _UNKNOWN (never set)
state.is_known("x")               # False

state.x = False
state.is_known("x")               # True (explicitly set to False)
state.get("x")                    # False

state.y = None
state.is_known("y")               # True (explicitly set to None)
state.get("y")                    # None

del state.x
state.is_known("x")               # False (deleted)
state.get("x")                    # returns _UNKNOWN
```

Use `is_known()` in sensors/logic to distinguish "never sensed" from "sensed as false".

### Methods

- **`update(other: Union[Dict, WorldState], **kwargs) -> None`**
  Updates the state with values from another state/dict or keyword arguments.
  Applies effect types (Set, Increment, Decrement, Unset) automatically.

- **`update_state(updates: dict[str, Any]) -> None`**
  Alias for `update()`.

- **`diff(other: WorldState) -> Dict[str, tuple[Any, Any]]`**
  Returns non-matching keys between self and other as `(self_val, other_val)`.

- **`copy(deep: bool = False) -> WorldState`**
  Returns a copy of the state.

- **`get(key: str, default: Any = _UNKNOWN) -> Any`**
  Safe accessor. Returns `_UNKNOWN` sentinel if key not set (distinct from None/False).

- **`is_known(key: str) -> bool`**
  Returns `True` if the attribute was explicitly set (even to None/False).

- **`to_dict() -> Dict[str, Any]`**
  Converts state to a standard dictionary.

- **`copy(deep: bool = False) -> WorldState`**
  Returns a copy of the state.

- **`get(key: str, default: Any = None) -> Any`**
  Safe accessor for state components.

- **`to_dict() -> Dict[str, Any]`**
  Converts state to a standard dictionary.
