# WorldState API Reference

The `WorldState` class represents the world state with attribute-style access. It inherits from `pydantic.BaseModel`.

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

::: goapauto.models.worldstate.WorldState
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false
