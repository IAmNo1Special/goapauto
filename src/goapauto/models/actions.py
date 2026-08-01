from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

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

    def to_dict(self) -> dict[str, Any]:
        """Convert predicate to a serializable dictionary."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Predicate:
        """Create a Predicate subclass instance from a dictionary."""
        if not isinstance(data, dict):
            raise TypeError("data must be a dictionary")
        op = data.get("op")
        if not op:
            raise ValueError("Dictionary missing 'op' field")
        registry: dict[str, type[Predicate]] = {
            "eq": Equal,
            "ne": NotEqual,
            "gt": GreaterThan,
            "lt": LessThan,
            "range": Range,
        }
        target_cls = registry.get(op)
        if not target_cls:
            raise ValueError(f"Unknown predicate op: {op}")
        kwargs = {k: v for k, v in data.items() if k != "op"}
        return target_cls(**kwargs)


class Equal(Predicate):
    """Predicate that checks if a value is equal to another."""

    op: Literal["eq"] = "eq"
    value: Any

    def __init__(self, value: Any) -> None:
        """Initialize the predicate, supporting positional arguments."""
        super().__init__(value=value, op="eq")

    def __call__(self, other: Any) -> bool:
        return other == self.value

    def __str__(self) -> str:
        return f"== {self.value}"


class NotEqual(Predicate):
    """Predicate that checks if a value is not equal to another."""

    op: Literal["ne"] = "ne"
    value: Any

    def __init__(self, value: Any) -> None:
        """Initialize the predicate, supporting positional arguments."""
        super().__init__(value=value, op="ne")

    def __call__(self, other: Any) -> bool:
        return other != self.value

    def __str__(self) -> str:
        return f"!= {self.value}"


class GreaterThan(Predicate):
    """Predicate that checks if a value is greater than another."""

    op: Literal["gt"] = "gt"
    value: int | float

    def __init__(self, value: int | float) -> None:
        """Initialize the predicate, supporting positional arguments."""
        super().__init__(value=value, op="gt")

    def __call__(self, other: Any) -> bool:
        return other > self.value

    def __str__(self) -> str:
        return f"> {self.value}"


class LessThan(Predicate):
    """Predicate that checks if a value is less than another."""

    op: Literal["lt"] = "lt"
    value: int | float

    def __init__(self, value: int | float) -> None:
        """Initialize the predicate, supporting positional arguments."""
        super().__init__(value=value, op="lt")

    def __call__(self, other: Any) -> bool:
        return other < self.value

    def __str__(self) -> str:
        return f"< {self.value}"


class Range(Predicate):
    """Predicate that checks if a value is within a range (inclusive)."""

    op: Literal["range"] = "range"
    min_value: int | float
    max_value: int | float

    def __init__(self, min_value: int | float, max_value: int | float) -> None:
        """Initialize the range predicate with inclusive bounds."""
        if min_value > max_value:
            raise ValueError("min_value must be <= max_value")
        super().__init__(min_value=min_value, max_value=max_value, op="range")

    def __call__(self, other: Any) -> bool:
        return self.min_value <= other <= self.max_value

    def __str__(self) -> str:
        return f"{self.min_value} <= x <= {self.max_value}"


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

    def to_dict(self) -> dict[str, Any]:
        """Convert effect to a serializable dictionary."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Effect:
        """Create an Effect subclass instance from a dictionary."""
        if not isinstance(data, dict):
            raise TypeError("data must be a dictionary")
        op = data.get("op")
        if not op:
            raise ValueError("Dictionary missing 'op' field")
        registry: dict[str, type[Effect]] = {
            "set": Set,
            "inc": Increment,
            "dec": Decrement,
            "unset": Unset,
        }
        target_cls = registry.get(op)
        if not target_cls:
            raise ValueError(f"Unknown effect op: {op}")
        kwargs = {k: v for k, v in data.items() if k != "op"}
        return target_cls(**kwargs)


class Set(Effect):
    """Effect that sets an attribute to a specific value."""

    op: Literal["set"] = "set"
    value: Any

    def __init__(self, value: Any) -> None:
        """Initialize the effect, supporting positional arguments."""
        super().__init__(value=value, op="set")

    def __call__(self, current_value: Any) -> Any:
        return self.value

    def __str__(self) -> str:
        return f"= {self.value}"


class Increment(Effect):
    """Effect that increments a numeric attribute."""

    op: Literal["inc"] = "inc"
    amount: int | float = 1

    def __init__(self, amount: int | float = 1) -> None:
        """Initialize the effect, supporting positional arguments."""
        super().__init__(amount=amount, op="inc")

    def __call__(self, current_value: Any) -> Any:
        return current_value + self.amount

    def __str__(self) -> str:
        return f"+= {self.amount}"


class Decrement(Effect):
    """Effect that decrements a numeric attribute."""

    op: Literal["dec"] = "dec"
    amount: int | float = 1

    def __init__(self, amount: int | float = 1) -> None:
        """Initialize the effect, supporting positional arguments."""
        super().__init__(amount=amount, op="dec")

    def __call__(self, current_value: Any) -> Any:
        return current_value - self.amount

    def __str__(self) -> str:
        return f"-= {self.amount}"


class Unset(Effect):
    """Effect that removes an attribute from the state."""

    op: Literal["unset"] = "unset"

    def __init__(self) -> None:
        """Initialize the unset effect."""
        super().__init__(op="unset")

    def __call__(self, current_value: Any) -> Any:
        # Return a sentinel to signal deletion
        return _UNSET_SENTINEL

    def __str__(self) -> str:
        return "unset"


# Sentinel object to signal attribute removal
_UNSET_SENTINEL = object()


# Public alias for Unset
Delete = Unset


@dataclass
class Action:
    """Represents an action the agent can take.

    Attributes:
        name: Unique identifier for the action
        preconditions: Dictionary of state requirements that must be met for the action to be applicable
        effects: Dictionary of state changes that result from applying this action
        cost: The cost of executing this action (used for pathfinding). Can be a single float
              or a dict/list for multi-dimensional costs (e.g., {"time": 30.0, "energy": 5.0}).
        duration: Optional duration in seconds (for temporal planning)
        description: Optional human-readable description of what the action does
    """

    name: str
    preconditions: dict[str, Any | Predicate | Callable[[Any], bool]]
    effects: dict[str, Any | Effect | Callable[[Any], Any]]
    cost: int | float | dict[str, float] | list[float] = 1
    duration: float | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        """Validate the action after initialization."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Action name must be a non-empty string")
        if not isinstance(self.preconditions, dict):
            raise TypeError("Preconditions must be a dictionary")
        if not isinstance(self.effects, dict):
            raise TypeError("Effects must be a dictionary")
        if not isinstance(self.cost, (int, float, dict, list)):
            raise TypeError("Cost must be a number, dict, or list")
        if isinstance(self.cost, (int, float)) and self.cost <= 0:
            raise ValueError("Cost must be positive")
        if isinstance(self.cost, dict):
            if not all(
                isinstance(v, (int, float)) and v >= 0 for v in self.cost.values()
            ):
                raise ValueError("All cost dict values must be non-negative numbers")
        if isinstance(self.cost, list):
            if not all(isinstance(v, (int, float)) and v >= 0 for v in self.cost):
                raise ValueError("All cost list values must be non-negative numbers")
        if self.duration is not None and not isinstance(self.duration, (int, float)):
            raise TypeError("Duration must be a number")
        if self.duration is not None and self.duration < 0:
            raise ValueError("Duration must be non-negative")
        if self.description is not None and not isinstance(self.description, str):
            raise TypeError("Description must be a string")

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
                    # Missing attributes are treated as 0 so effects
                    # like Increment/Decrement can create new attributes
                    current_val = getattr(state, attr, 0)
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
                    current_val = getattr(state, attr, 0)
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
        desc = f", description='{self.description}'" if self.description else ""
        return (
            f"{self.__class__.__name__}('{self.name}', "
            f"preconditions={self.preconditions}, "
            f"effects={self.effects}, cost={self.cost}{desc})"
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
        self._actions: list[Action] = []

    def add_action(
        self,
        name: str,
        preconditions: dict[str, Any],
        effects: dict[str, Any],
        cost: int | float | dict[str, float] | list[float] = 1,
        duration: float | None = None,
        description: str | None = None,
    ) -> None:
        """Add a single action to the collection.

        Args:
            name: Unique identifier for the action
            preconditions: Dictionary of state requirements for the action
            effects: Dictionary of state changes caused by the action
            cost: The cost of executing this action (default: 1)
            duration: Optional duration in seconds
            description: Optional human-readable description

        Raises:
            ValueError: If an action with the same name already exists
            TypeError: If any parameter has an invalid type
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Action name must be a non-empty string")

        if self.get_action(name) is not None:
            raise ValueError(f"Action with name '{name}' already exists")

        try:
            action = Action(
                name,
                preconditions,
                effects,
                cost,
                duration=duration,
                description=description,
            )
            self._actions.append(action)
            logger.debug("Added action: %s", name)
        except Exception as e:
            logger.error("Failed to add action %s: %s", name, str(e))
            raise

    def add_actions(
        self, action_definitions: Sequence[tuple[Any, ...] | Action]
    ) -> None:
        """Add multiple actions to the collection.


        Args:
            action_definitions: List of action definitions where each definition is an
                Action object or a tuple in format:
                - (name, preconditions, effects, cost)
                - (name, preconditions, effects, cost, duration)
                - (name, preconditions, effects, cost, description)
                - (name, preconditions, effects, cost, duration, description)

        Example:
            actions = Actions()
            actions.add_actions([
                ("open_door", {"door_locked": False}, {"door_open": True}, 1),
                ("unlock_door", {"has_key": True}, {"door_locked": False}, 2, "Unlocks door")
            ])
        """
        if not isinstance(action_definitions, (list, tuple)):
            raise TypeError("action_definitions must be a list or tuple")

        for i, action_def in enumerate(action_definitions):
            try:
                if isinstance(action_def, Action):
                    if self.get_action(action_def.name) is not None:
                        raise ValueError(
                            f"Action with name '{action_def.name}' already exists"
                        )
                    self._actions.append(action_def)
                elif isinstance(action_def, (list, tuple)):
                    n = len(action_def)
                    if n == 4:
                        self.add_action(*action_def)
                    elif n == 5:
                        fifth = action_def[4]
                        if isinstance(fifth, str):
                            self.add_action(
                                action_def[0],
                                action_def[1],
                                action_def[2],
                                action_def[3],
                                description=fifth,
                            )
                        else:
                            self.add_action(
                                action_def[0],
                                action_def[1],
                                action_def[2],
                                action_def[3],
                                duration=fifth,
                            )
                    elif n == 6:
                        self.add_action(
                            action_def[0],
                            action_def[1],
                            action_def[2],
                            action_def[3],
                            duration=action_def[4],
                            description=action_def[5],
                        )
                    else:
                        raise ValueError(
                            f"Action definition tuple at index {i} must have length 4, 5, or 6"
                        )
                else:
                    raise ValueError(
                        f"Action definition at index {i} must be an Action object or a tuple"
                    )
            except Exception as e:
                logger.error("Error adding action at index %d: %s", i, str(e))
                raise

    def get_action(self, name: str) -> Action | None:
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

    def get_actions(self) -> list[Action]:
        """Get a list of all actions in the collection.

        Returns:
            A new list containing all Action objects
        """
        return self._actions.copy()

    def clear_actions(self) -> None:
        """Remove all actions from the collection."""
        self._actions.clear()
        logger.info("Cleared all actions")

    def filter_actions(self, state: Any) -> list[Action]:
        """Get a list of all actions that can be applied to the given state.

        Args:
            state: The state to check against action preconditions

        Returns:
            A list of applicable Action objects
        """
        return [action for action in self._actions if action.is_applicable(state)]

    def __iter__(self) -> Iterator[Action]:
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
