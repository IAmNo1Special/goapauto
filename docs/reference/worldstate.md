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

### Methods

- **`update(other: Union[Dict, WorldState], **kwargs) -> None`**
  Updates the state with values from another state/dict or keyword arguments.

- **`diff(other: WorldState) -> Dict[str, tuple[Any, Any]]`**
  Returns non-matching keys between self and other as `(self_val, other_val)`.

- **`copy(deep: bool = False) -> WorldState`**
  Returns a copy of the state.

- **`get(key: str, default: Any = None) -> Any`**
  Safe accessor for state components.

- **`to_dict() -> Dict[str, Any]`**
  Converts state to a standard dictionary.
