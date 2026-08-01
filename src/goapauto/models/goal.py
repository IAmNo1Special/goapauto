from __future__ import annotations

import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)
T = TypeVar("T", bound="Goal")


class Goal(BaseModel):
    """Represents a goal with a target state and optional priority.

    A goal defines a desired state that the planner should try to achieve.
    Goals can have different priorities, with lower numbers indicating
    higher priority (e.g., priority 1 is higher than priority 2).
    """

    target_state: dict[str, Any]
    priority: int = Field(default=1, ge=1)
    name: str | None = None

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        validate_assignment=True,
    )

    @field_validator("target_state")
    @classmethod
    def validate_target_state(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            raise ValueError("target_state cannot be empty")
        return v

    def model_post_init(self, __context: Any) -> None:
        """Initialize name if not provided."""
        if self.name is None:
            self.name = str(self.target_state)

    def is_satisfied(self, world_state: Any) -> bool:
        """Check if this goal is satisfied by the given world state.

        Args:
            world_state: The world state to check against.

        Returns:
            bool: True if all conditions in target_state are satisfied.
        """
        try:
            for attr, desired in self.target_state.items():
                current = getattr(world_state, attr, None)
                if callable(desired):
                    if not desired(current):
                        return False
                elif current != desired:
                    return False
            return True
        except AttributeError as e:
            logger.error("Error checking goal satisfaction: %s", str(e))
            return False

    def get_unsatisfied_conditions(
        self, world_state: Any
    ) -> dict[str, tuple[Any, Any]]:
        """Get the conditions that are not satisfied in the current world state."""
        try:
            unsatisfied = {}
            for attr, desired in self.target_state.items():
                current = getattr(world_state, attr, None)
                if callable(desired):
                    if not desired(current):
                        unsatisfied[attr] = (current, desired)
                elif current != desired:
                    unsatisfied[attr] = (current, desired)
            return unsatisfied
        except Exception as e:
            logger.error("Error getting unsatisfied conditions: %s", str(e))
            raise

    def __hash__(self) -> int:
        """Compute a hash value for this goal."""
        return hash((frozenset(self.target_state.items()), self.priority, self.name))

    def __str__(self) -> str:
        """Return a string representation of the goal."""
        return f"{self.__class__.__name__}({self.name}, priority={self.priority})"

    def __repr__(self) -> str:
        """Return a detailed string representation of the goal."""
        return (
            f"<{self.__class__.__name__} "
            f"name='{self.name}', "
            f"priority={self.priority}, "
            f"target_state={self.target_state}>"
        )
