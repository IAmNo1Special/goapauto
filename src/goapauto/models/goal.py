from __future__ import annotations
import logging
from typing import Any, Dict, Optional, Type, TypeVar, Union
from dataclasses import dataclass, field
from copy import deepcopy

logger = logging.getLogger(__name__)
T = TypeVar('T', bound='Goal')

class Goal:
    """Represents a goal with a target state and optional priority.
    
    A goal defines a desired state that the planner should try to achieve.
    Goals can have different priorities, with lower numbers indicating
    higher priority (e.g., priority 1 is higher than priority 2).
    
    Attributes:
        target_state: A dictionary mapping state attributes to desired values
        priority: The priority level (lower number = higher priority)
        name: A human-readable name for the goal (defaults to string representation of target_state)
    """
    
    def __init__(self, 
                target_state: Dict[str, Any], 
                priority: int = 1, 
                name: Optional[str] = None):
        """Initialize a new Goal instance.
        
        Args:
            target_state: Dictionary mapping state attributes to desired values.
                         Must be a non-empty dictionary.
            priority: Priority level (lower number = higher priority). Must be >= 1.
            name: Optional name for the goal. If not provided, will use string
                  representation of target_state.
                  
        Raises:
            ValueError: If target_state is empty or priority is invalid.
            TypeError: If target_state is not a dictionary or priority is not an integer.
        """
        if not isinstance(target_state, dict):
            raise TypeError(f"target_state must be a dictionary, got {type(target_state)}")
        if not target_state:
            raise ValueError("target_state cannot be empty")
        if not isinstance(priority, int):
            raise TypeError(f"priority must be an integer, got {type(priority)}")
        if priority < 1:
            raise ValueError(f"priority must be >= 1, got {priority}")
            
        self._target_state = target_state.copy()  # Store a copy to prevent external modification
        self._priority = priority
        self._name = name or str(target_state)
    
    @property
    def target_state(self) -> Dict[str, Any]:
        """Get the target state dictionary (read-only)."""
        return self._target_state.copy()
    
    @property
    def priority(self) -> int:
        """Get the priority of this goal (read-only)."""
        return self._priority
    
    @property
    def name(self) -> str:
        """Get the name of this goal (read-only)."""
        return self._name
    
    def is_satisfied(self, world_state: Any) -> bool:
        """Check if this goal is satisfied by the given world state.
        
        Args:
            world_state: The world state to check against. Should be an object with
                       attributes matching the target_state keys.
                       
        Returns:
            bool: True if all conditions in target_state are satisfied, False otherwise.
            
        Raises:
            AttributeError: If world_state is not an object with attributes.
        """
        if not hasattr(world_state, '__dict__'):
            raise TypeError("world_state must be an object with attributes")
            
        try:
            return all(
                getattr(world_state, attr) == value 
                for attr, value in self._target_state.items()
            )
        except AttributeError as e:
            logger.error("Error checking goal satisfaction: %s", str(e))
            return False
    
    def get_unsatisfied_conditions(self, world_state: Any) -> Dict[str, tuple[Any, Any]]:
        """Get the conditions that are not satisfied in the current world state.
        
        Args:
            world_state: The world state to check against.
            
        Returns:
            A dictionary mapping attribute names to tuples of (current_value, desired_value)
            for all conditions that are not satisfied.
            
        Raises:
            AttributeError: If world_state is not an object with attributes.
        """
        if not hasattr(world_state, '__dict__'):
            raise TypeError("world_state must be an object with attributes")
            
        try:
            return {
                attr: (getattr(world_state, attr, None), desired_value)
                for attr, desired_value in self._target_state.items()
                if getattr(world_state, attr, None) != desired_value
            }
        except Exception as e:
            logger.error("Error getting unsatisfied conditions: %s", str(e))
            raise
    
    def copy(self: T) -> T:
        """Create a deep copy of this goal.
        
        Returns:
            A new Goal instance with the same target_state, priority, and name.
        """
        return self.__class__(
            target_state=deepcopy(self._target_state),
            priority=self._priority,
            name=self._name
        )
    
    def __eq__(self, other: object) -> bool:
        """Check if two goals are equal.
        
        Two goals are considered equal if they have the same target_state,
        priority, and name.
        """
        if not isinstance(other, Goal):
            return NotImplemented
            
        return (
            self._target_state == other._target_state
            and self._priority == other._priority
            and self._name == other._name
        )
    
    def __hash__(self) -> int:
        """Compute a hash value for this goal."""
        return hash((
            frozenset(self._target_state.items()),
            self._priority,
            self._name
        ))
    
    def __str__(self) -> str:
        """Return a string representation of the goal."""
        return f"{self.__class__.__name__}({self._name}, priority={self._priority})"
    
    def __repr__(self) -> str:
        """Return a detailed string representation of the goal."""
        return (
            f"<{self.__class__.__name__} "
            f"name='{self._name}', "
            f"priority={self._priority}, "
            f"target_state={self._target_state}>"
        )
