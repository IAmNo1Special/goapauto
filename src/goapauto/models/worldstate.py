from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, Type, TypeVar, Union

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)
T = TypeVar("T", bound="WorldState")


class WorldState(BaseModel):
    """A class representing the world state with attribute-style access.

    This class uses Pydantic for validation and type safety. It provides a
    dictionary-like interface and attribute-style access to state values.
    """

    model_config = ConfigDict(
        extra="allow",
        arbitrary_types_allowed=True,
        validate_assignment=True,
    )

    # Custom __init__ removed to enforce strict keyword-only Pydantic API.

    def __getitem__(self, key: str) -> Any:
        """Get a state value using dictionary access."""
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        """Set a state value using dictionary access."""
        setattr(self, key, value)

    def __contains__(self, key: str) -> bool:
        """Check if a state key exists."""
        return key in self.__dict__

    def __iter__(self) -> Iterator[str]:
        """Iterate over state keys."""
        return iter(self.__dict__)

    def items(self) -> Any:
        """Return a view of (key, value) pairs."""
        return self.__dict__.items()

    def keys(self) -> Any:
        """Return a view of state keys."""
        return self.__dict__.keys()

    def values(self) -> Any:
        """Return a view of state values."""
        return self.__dict__.values()

    def get(self, key: str, default: Any = None) -> Any:
        """Get a state value with a default if it doesn't exist."""
        return getattr(self, key, default)

    def update(self, other: Union[Dict[str, Any], "WorldState"], **kwargs: Any) -> None:
        """Update the state with values from a dictionary or another WorldState."""
        if isinstance(other, WorldState):
            updates = other.model_dump()
        else:
            updates = other

        for k, v in updates.items():
            setattr(self, k, v)
        for k, v in kwargs.items():
            setattr(self, k, v)

    def clear(self) -> None:
        """Clear all state values."""
        # Pydantic models aren't really meant to be cleared,
        # but we can clear the __dict__ if it's dynamic
        self.__dict__.clear()

    def copy(self: T, deep: bool = False) -> T:
        """Create a copy of this WorldState."""
        return super().model_copy(deep=deep)

    def __hash__(self) -> int:
        """Compute a hash value for this state."""
        # Use model_dump to get a stable dictionary for hashing
        items = self.model_dump().items()
        return hash(frozenset(items))

    def __len__(self) -> int:
        """Get the number of state values."""
        return len(self.__dict__)

    def __bool__(self) -> bool:
        """Check if the state is non-empty."""
        return bool(self.__dict__)

    def get_state(self) -> Dict[str, Any]:
        """Get a copy of the current state as a dictionary."""
        return self.model_dump()

    def update_state(self, updates: Dict[str, Any]) -> None:
        """Update multiple state values at once."""
        self.update(updates)

    def to_dict(self) -> Dict[str, Any]:
        """Convert the state to a dictionary."""
        return self.model_dump()

    @classmethod
    def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
        """Create a new WorldState from a dictionary."""
        return cls(**data)

    def diff(self, other: "WorldState") -> Dict[str, tuple[Any, Any]]:
        """Get the differences between this state and another."""
        if not isinstance(other, WorldState):
            raise TypeError(
                f"Cannot diff with {type(other).__name__}, expected WorldState"
            )

        differences = {}
        s1 = self.model_dump()
        s2 = other.model_dump()
        all_keys = set(s1.keys()) | set(s2.keys())

        for key in all_keys:
            v1 = s1.get(key)
            v2 = s2.get(key)
            if v1 != v2:
                differences[key] = (v1, v2)

        return differences
