import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)
T = TypeVar("T", bound="Action")


class Predicate(BaseModel, ABC):
    """Base class for state predicates.

    Predicates are used in preconditions to check if a state value
    meets certain criteria.
    """

    model_config = ConfigDict(frozen=True)

    @abstractmethod
    def __call__(self, value: Any) -> bool:
        """Evaluate the predicate against a value."""
        pass


class Equal(Predicate):
    """Predicate that checks if a value is equal to another."""

    value: Any

    def __call__(self, other: Any) -> bool:
        return other == self.value

    def __str__(self) -> str:
        return f"== {self.value}"


class NotEqual(Predicate):
    """Predicate that checks if a value is not equal to another."""

    value: Any

    def __call__(self, other: Any) -> bool:
        return other != self.value

    def __str__(self) -> str:
        return f"!= {self.value}"


class GreaterThan(Predicate):
    """Predicate that checks if a value is greater than another."""

    value: Union[int, float]

    def __call__(self, other: Any) -> bool:
        return other > self.value

    def __str__(self) -> str:
        return f"> {self.value}"


class LessThan(Predicate):
    """Predicate that checks if a value is less than another."""

    value: Union[int, float]

    def __call__(self, other: Any) -> bool:
        return other < self.value

    def __str__(self) -> str:
        return f"< {self.value}"


class Effect(BaseModel, ABC):
    """Base class for state effects.

    Effects are used to define how a state attribute changes
    when an action is applied.
    """

    model_config = ConfigDict(frozen=True)

    @abstractmethod
    def __call__(self, current_value: Any) -> Any:
        """Compute the new value based on the current value."""
        pass


class Set(Effect):
    """Effect that sets an attribute to a specific value."""

    value: Any

    def __call__(self, current_value: Any) -> Any:
        return self.value

    def __str__(self) -> str:
        return f"= {self.value}"


class Increment(Effect):
    """Effect that increments a numeric attribute."""

    amount: Union[int, float] = 1

    def __call__(self, current_value: Any) -> Any:
        return current_value + self.amount

    def __str__(self) -> str:
        return f"+= {self.amount}"


class Decrement(Effect):
    """Effect that decrements a numeric attribute."""

    amount: Union[int, float] = 1

    def __call__(self, current_value: Any) -> Any:
        return current_value - self.amount

    def __str__(self) -> str:
        return f"-= {self.amount}"


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
    preconditions: Dict[str, Union[Any, Predicate, Callable[[Any], bool]]]
    effects: Dict[str, Union[Any, Effect, Callable[[Any], Any]]]
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
        logger.debug("Checking applicability of action: %s", self.name)
        try:
            for attr, expected in self.preconditions.items():
                if not hasattr(state, attr):
                    logger.debug("State missing required attribute: %s", attr)
                    return False

                current_value = getattr(state, attr)

                # Handle Predicate objects or other callables
                if callable(expected):
                    if not expected(current_value):
                        logger.debug(
                            "Precondition failed for %s: %s(%s) is False",
                            attr,
                            expected,
                            current_value,
                        )
                        return False
                # Handle direct value comparison
                elif current_value != expected:
                    logger.debug(
                        "Precondition not met: %s != %s",
                        current_value,
                        expected,
                    )
                    return False
            return True
        except Exception as e:
            logger.error(
                "Error checking action applicability: %s", str(e), exc_info=True
            )
            return False

    def apply(self, state: Any) -> Any:
        """Apply this action to the current state and return a new state.

        Args:
            state: The current world state to apply the action to

        Returns:
            A new state with the action's effects applied
        """
        if not self.is_applicable(state):
            raise ValueError(
                f"Action {self.name} is not applicable to the current state"
            )

        logger.info("Applying action: %s", self.name)
        try:
            # Create a copy of the state
            # WorldState (Pydantic) has a copy() method
            new_state = state.copy(deep=True)

            # Apply each effect to the new state
            for attr, effect in self.effects.items():
                if callable(effect):
                    # For callable effects (including Effect objects),
                    # pass the current attribute value
                    current_val = getattr(state, attr)
                    setattr(new_state, attr, effect(current_val))
                else:
                    setattr(new_state, attr, effect)

            logger.debug("New state after %s: %s", self.name, new_state)
            return new_state

        except Exception as e:
            logger.error(
                "Failed to apply action %s: %s", self.name, str(e), exc_info=True
            )
            raise

    async def async_apply(self, state: Any) -> Any:
        """Asynchronously apply this action to the current state and return a new state.

        Args:
            state: The current world state to apply the action to

        Returns:
            A new state with the action's effects applied
        """
        if not self.is_applicable(state):
            raise ValueError(
                f"Action {self.name} is not applicable to the current state"
            )

        logger.info("Applying action asynchronously: %s", self.name)
        try:
            # Create a copy of the state
            new_state = state.copy(deep=True)

            # Apply each effect to the new state
            for attr, effect in self.effects.items():
                if callable(effect):
                    current_val = getattr(state, attr)
                    import inspect

                    if inspect.iscoroutinefunction(effect):
                        setattr(new_state, attr, await effect(current_val))
                    else:
                        setattr(new_state, attr, effect(current_val))
                else:
                    setattr(new_state, attr, effect)

            logger.debug("New state after async %s: %s", self.name, new_state)
            return new_state

        except Exception as e:
            logger.error(
                "Failed to async apply action %s: %s", self.name, str(e), exc_info=True
            )
            raise

    def __str__(self) -> str:
        """Return a string representation of the action."""
        return (
            f"{self.__class__.__name__}('{self.name}', "
            f"preconditions={self.preconditions}, "
            f"effects={self.effects}, cost={self.cost})"
        )

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

    def add_action(
        self,
        name: str,
        preconditions: Dict[str, Any],
        effects: Dict[str, Any],
        cost: int = 1,
    ) -> None:
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
            logger.debug("Added action: %s", name)
        except Exception as e:
            logger.error("Failed to add action %s: %s", name, str(e))
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
                    raise ValueError(
                        f"Action definition at index {i} must be a 4-tuple "
                        "(name, preconditions, effects, cost)"
                    )
                self.add_action(*action_def)
            except Exception as e:
                logger.error("Error adding action at index %d: %s", i, str(e))
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
        logger.info("Cleared all actions")

    def filter_actions(self, state: Any) -> List[Action]:
        """Get a list of all actions that can be applied to the given state.

        Args:
            state: The state to check against action preconditions

        Returns:
            A list of applicable Action objects
        """
        return [action for action in self._actions if action.is_applicable(state)]

    def __iter__(self) -> "Actions":
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
