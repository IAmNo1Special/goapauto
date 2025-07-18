from __future__ import annotations
from typing import Any, Dict, Optional, Type, TypeVar, Union, overload, Iterator, ItemsView, KeysView, ValuesView
import copy
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)
T = TypeVar('T', bound='WorldState')

class WorldState:
    """A class representing the world state with attribute-style access.
    
    This class provides a dictionary-like interface with attribute-style access
    to state values. It's designed to be used as the state representation in
    planning and decision-making systems.
    
    Attributes:
        _state: Internal dictionary storing the state values
    """
    
    def __init__(self, initial_state: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        """Initialize a new WorldState instance.
        
        Args:
            initial_state: Optional dictionary of initial state values
            **kwargs: Additional state values as keyword arguments
            
        Example:
            >>> state = WorldState({"is_open": True}, is_active=False)
            >>> state.is_open  # True
            >>> state.is_active  # False
        """
        # Initialize with empty state
        self._state: Dict[str, Any] = {}
        
        # Update with initial_state if provided
        if initial_state is not None:
            if not isinstance(initial_state, dict):
                raise TypeError(f"initial_state must be a dictionary, got {type(initial_state)}")
            self._state.update(initial_state)
            
        # Always update with any additional kwargs
        self._state.update(kwargs)
    
    def __getattr__(self, name: str) -> Any:
        """Get a state value using attribute access.
        
        Args:
            name: The name of the state attribute to get
            
        Returns:
            The value of the state attribute
            
        Raises:
            AttributeError: If the attribute doesn't exist
        """
        if name == '_state':
            # This prevents infinite recursion during deepcopy
            return object.__getattribute__(self, '_state')
            
        try:
            return self._state[name]
        except KeyError as e:
            raise AttributeError(
                f"'{self.__class__.__name__}' object has no attribute '{name}'") from e
    
    def __setattr__(self, name: str, value: Any) -> None:
        """Set a state value using attribute access.
        
        Args:
            name: The name of the state attribute to set
            value: The value to set
        """
        if name == '_state':
            object.__setattr__(self, name, value)
        else:
            self._state[name] = value
    
    def __delattr__(self, name: str) -> None:
        """Delete a state value using attribute access.
        
        Args:
            name: The name of the state attribute to delete
            
        Raises:
            AttributeError: If the attribute doesn't exist
        """
        if name in self._state:
            del self._state[name]
        else:
            raise AttributeError(
                f"'{self.__class__.__name__}' object has no attribute '{name}'")
    
    def __contains__(self, key: str) -> bool:
        """Check if a state key exists.
        
        Args:
            key: The key to check
            
        Returns:
            bool: True if the key exists, False otherwise
        """
        return key in self._state
    
    def __iter__(self) -> Iterator[str]:
        """Iterate over state keys."""
        return iter(self._state)
    
    def items(self) -> ItemsView[str, Any]:
        """Return a view of (key, value) pairs."""
        return self._state.items()
    
    def keys(self) -> KeysView[str]:
        """Return a view of state keys."""
        return self._state.keys()
    
    def values(self) -> ValuesView[Any]:
        """Return a view of state values."""
        return self._state.values()
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a state value with a default if it doesn't exist.
        
        Args:
            key: The key to get
            default: The default value to return if the key doesn't exist
            
        Returns:
            The value for the key, or default if the key doesn't exist
        """
        return self._state.get(key, default)
    
    def update(self, other: Union[Dict[str, Any], 'WorldState'], **kwargs: Any) -> None:
        """Update the state with values from a dictionary or another WorldState.
        
        Args:
            other: A dictionary or WorldState to update from
            **kwargs: Additional key-value pairs to update
        """
        if isinstance(other, WorldState):
            self._state.update(other._state)
        else:
            self._state.update(other)
        self._state.update(kwargs)
    
    def clear(self) -> None:
        """Clear all state values."""
        self._state.clear()
    
    def copy(self: T) -> T:
        """Create a shallow copy of this WorldState.
        
        Returns:
            A new WorldState with the same state values
        """
        return self.__class__(self._state.copy())
    
    def __str__(self) -> str:
        """Return a string representation of the state."""
        return f"{self.__class__.__name__}({self._state})"
    
    def __repr__(self) -> str:
        """Return a detailed string representation of the state."""
        return f"<{self.__class__.__name__} state={self._state}>"
    
    def __eq__(self, other: object) -> bool:
        """Check if this state is equal to another.
        
        Args:
            other: The other object to compare with
            
        Returns:
            bool: True if the states are equal, False otherwise
        """
        if not isinstance(other, WorldState):
            return False
        return self._state == other._state
    
    def __hash__(self) -> int:
        """Compute a hash value for this state."""
        return hash(frozenset(self._state.items()))
    
    def __len__(self) -> int:
        """Get the number of state values."""
        return len(self._state)
    
    def __bool__(self) -> bool:
        """Check if the state is non-empty."""
        return bool(self._state)
    
    def __deepcopy__(self: T, memo: Optional[dict] = None) -> T:
        """Create a deep copy of this WorldState.
        
        Args:
            memo: Dictionary of objects already copied
            
        Returns:
            A deep copy of this WorldState
        """
        if memo is None:
            memo = {}
            
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        
        # Deep copy the state dictionary
        result._state = copy.deepcopy(self._state, memo)
        
        return result
    
    def get_state(self) -> Dict[str, Any]:
        """Get a copy of the current state as a dictionary.
        
        Returns:
            A copy of the internal state dictionary
        """
        return self._state.copy()
    
    def update_state(self, updates: Dict[str, Any]) -> None:
        """Update multiple state values at once.
        
        Args:
            updates: Dictionary of updates to apply
        """
        if not isinstance(updates, dict):
            raise TypeError(f"updates must be a dictionary, got {type(updates)}")
        self._state.update(updates)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert the state to a dictionary.
        
        Returns:
            A copy of the internal state dictionary
        """
        return self.get_state()
    
    @classmethod
    def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
        """Create a new WorldState from a dictionary.
        
        Args:
            data: Dictionary of initial state values
            
        Returns:
            A new WorldState instance
        """
        return cls(data)
    
    def diff(self, other: 'WorldState') -> Dict[str, tuple[Any, Any]]:
        """Get the differences between this state and another.
        
        Args:
            other: The other WorldState to compare with
            
        Returns:
            A dictionary of differences in the format {key: (self_value, other_value)}
        """
        if not isinstance(other, WorldState):
            raise TypeError(f"Cannot diff with {type(other).__name__}, expected WorldState")
            
        differences = {}
        all_keys = set(self._state.keys()) | set(other._state.keys())
        
        for key in all_keys:
            if key not in self._state:
                differences[key] = (None, other._state[key])
            elif key not in other._state:
                differences[key] = (self._state[key], None)
            elif self._state[key] != other._state[key]:
                differences[key] = (self._state[key], other._state[key])
                
        return differences
