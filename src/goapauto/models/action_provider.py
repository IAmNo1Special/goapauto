from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from goapauto.models.actions import Action, Actions
from goapauto.models.worldstate import WorldState


@runtime_checkable
class ActionProvider(Protocol):
    """Protocol for dynamic action providers.

    Action providers allow systems to dynamically provide actions
    available to the planner based on the current world state.
    """

    def provide_actions(self, state: WorldState) -> List[Action]:
        """Provide a list of actions available for the given state.

        Args:
            state: The current world state

        Returns:
            A list of Action objects
        """
        ...


class StaticActionProvider:
    """An action provider that wraps a static collection of actions."""

    def __init__(self, actions: Actions) -> None:
        self.actions = actions

    def provide_actions(self, state: WorldState) -> List[Action]:
        return self.actions.get_actions()
