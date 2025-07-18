import copy
import logging
from typing import Any, Dict, List, Optional, Type, TypeVar, Union
from dataclasses import dataclass

from models.worldstate import WorldState

logger = logging.getLogger(__name__)
T = TypeVar('T', bound='Action')

@dataclass
class Action:
    """Represents an action the agent can take.
    
    Attributes:
        name: Unique identifier for the action
        preconditions: Dictionary of state requirements that must be met for the action to be applicable
        effects: Dictionary of state changes that result from applying this action
        cost: The cost of executing this action (used for pathfinding)
    """
    name: str
    preconditions: Dict[str, Any]
    effects: Dict[str, Any]
    cost: int = 1
    
    def __post_init__(self) -> None:
        """Validate the action after initialization."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Action name must be a non-empty string")
        if not isinstance(self.preconditions, dict):
            raise TypeError("Preconditions must be a dictionary")
        if not isinstance(self.effects, dict):
            raise TypeError("Effects must be a dictionary")
        if not isinstance(self.cost, (int, float)) or self.cost <= 0:
            raise ValueError("Cost must be a positive number")

    def is_applicable(self, state: Any) -> bool:
        """Check if this action can be applied to the given state.
        
        Args:
            state: The current world state to check against
            
        Returns:
            bool: True if all preconditions are met, False otherwise
        """
        if not hasattr(state, '__dict__'):
            raise TypeError("State must be an object with attributes")
            
        logger.debug('Checking applicability of action: %s', self.name)
        try:
            for attr, expected_value in self.preconditions.items():
                if not hasattr(state, attr):
                    logger.debug('State missing required attribute: %s', attr)
                    return False
                if getattr(state, attr) != expected_value:
                    logger.debug('Precondition not met: %s != %s', 
                               getattr(state, attr), expected_value)
                    return False
            return True
        except Exception as e:
            logger.error('Error checking action applicability: %s', str(e), exc_info=True)
            return False

    def apply(self, state: Any) -> Any:
        """Apply this action to the current state and return a new state.
        
        Args:
            state: The current world state to apply the action to
            
        Returns:
            A new state with the action's effects applied
            
        Raises:
            ValueError: If the action cannot be applied to the current state
        """
        if not self.is_applicable(state):
            raise ValueError(f"Action {self.name} is not applicable to the current state")
            
        logger.info('Applying action: %s', self.name)
        try:
            # Create a deep copy to avoid modifying the original state
            new_state = copy.deepcopy(state)
            
            # Apply each effect to the new state
            for attr, value in self.effects.items():
                setattr(new_state, attr, value)
                
            logger.debug('New state after %s: %s', self.name, new_state)
            return new_state
            
        except Exception as e:
            logger.error('Failed to apply action %s: %s', self.name, str(e), exc_info=True)
            raise

    def __str__(self) -> str:
        """Return a string representation of the action."""
        return (f"{self.__class__.__name__}('{self.name}', "
                f"preconditions={self.preconditions}, "
                f"effects={self.effects}, cost={self.cost})")
                
    def __repr__(self) -> str:
        """Return the canonical string representation of the action."""
        return str(self)

class Actions:
    """Manages a collection of available actions for the GOAP planner.
    
    This class provides methods to add, retrieve, and manage actions that can be
    used by the planner to achieve goals. It ensures that all actions are valid
    and provides efficient lookup and iteration capabilities.
    """
    
    def __init__(self) -> None:
        """Initialize an empty collection of actions."""
        self._actions: List[Action] = []

    def add_action(self, name: str, preconditions: Dict[str, Any], 
                  effects: Dict[str, Any], cost: int = 1) -> None:
        """Add a single action to the collection.
        
        Args:
            name: Unique identifier for the action
            preconditions: Dictionary of state requirements for the action
            effects: Dictionary of state changes caused by the action
            cost: The cost of executing this action (default: 1)
            
        Raises:
            ValueError: If an action with the same name already exists
            TypeError: If any parameter has an invalid type
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Action name must be a non-empty string")
            
        if self.get_action(name) is not None:
            raise ValueError(f"Action with name '{name}' already exists")
            
        try:
            action = Action(name, preconditions, effects, cost)
            self._actions.append(action)
            logger.debug('Added action: %s', name)
        except Exception as e:
            logger.error('Failed to add action %s: %s', name, str(e))
            raise
    
    def add_actions(self, action_definitions: List[tuple]) -> None:
        """Add multiple actions to the collection.
        
        Args:
            action_definitions: List of action definitions where each definition is a tuple
                in the format (name: str, preconditions: dict, effects: dict, cost: int)
                
        Example:
            actions = Actions()
            actions.add_actions([
                ("open_door", {"door_locked": False}, {"door_open": True}, 1),
                ("unlock_door", {"has_key": True}, {"door_locked": False}, 2)
            ])
        """
        if not isinstance(action_definitions, (list, tuple)):
            raise TypeError("action_definitions must be a list or tuple")
            
        for i, action_def in enumerate(action_definitions):
            try:
                if not isinstance(action_def, (list, tuple)) or len(action_def) != 4:
                    raise ValueError(f"Action definition at index {i} must be a 4-tuple "
                                    "(name, preconditions, effects, cost)")
                self.add_action(*action_def)
            except Exception as e:
                logger.error('Error adding action at index %d: %s', i, str(e))
                raise
    
    def get_action(self, name: str) -> Optional[Action]:
        """Retrieve an action by its name.
        
        Args:
            name: The name of the action to retrieve
            
        Returns:
            The Action object if found, None otherwise
        """
        if not isinstance(name, str):
            raise TypeError("Action name must be a string")
            
        for action in self._actions:
            if action.name == name:
                return action
        return None
    
    def get_actions(self) -> List[Action]:
        """Get a list of all actions in the collection.
        
        Returns:
            A new list containing all Action objects
        """
        return self._actions.copy()
    
    def clear_actions(self) -> None:
        """Remove all actions from the collection."""
        self._actions.clear()
        logger.info('Cleared all actions')
    
    def filter_actions(self, state: Any) -> List[Action]:
        """Get a list of all actions that can be applied to the given state.
        
        Args:
            state: The state to check against action preconditions
            
        Returns:
            A list of applicable Action objects
        """
        return [action for action in self._actions if action.is_applicable(state)]
    
    def __iter__(self) -> 'Actions':
        """Return an iterator over all actions."""
        return iter(self._actions)
    
    def __len__(self) -> int:
        """Return the number of actions in the collection."""
        return len(self._actions)
    
    def __contains__(self, name: str) -> bool:
        """Check if an action with the given name exists."""
        return self.get_action(name) is not None
    
    def __str__(self) -> str:
        """Return a string representation of the actions collection."""
        return f"Actions({len(self._actions)} actions available)"
    
    def __repr__(self) -> str:
        """Return a detailed string representation of the actions collection."""
        return f"<{self.__class__.__name__} with {len(self._actions)} actions>"
