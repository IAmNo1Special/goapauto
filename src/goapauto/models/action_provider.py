from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from goapauto.models.actions import Action, Actions
from goapauto.models.goal import Goal
from goapauto.models.worldstate import WorldState


@runtime_checkable
class ActionProvider(Protocol):
    """Protocol for dynamic action providers.

    Action providers allow systems to dynamically provide actions
    available to the planner based on the current world state and goal.
    """

    def provide_actions(
        self, state: WorldState, goal: Goal | dict[str, Any] | None = None
    ) -> list[Action]:
        """Provide a list of actions available for the given state and goal.

        Args:
            state: The current world state
            goal: Optional goal the planner is trying to achieve

        Returns:
            A list of Action objects
        """
        ...


class StaticActionProvider:
    """An action provider that wraps a static collection of actions."""

    def __init__(self, actions: Actions) -> None:
        self.actions = actions

    def provide_actions(
        self, state: WorldState, goal: Goal | dict[str, Any] | None = None
    ) -> list[Action]:
        return self.actions.get_actions()
