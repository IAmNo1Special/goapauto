from __future__ import annotations

from collections.abc import Callable, Iterable
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


ActionFunction = Callable[
    [WorldState, "Goal | dict[str, Any] | None"],
    "Iterable[Action] | Actions | Action | None",
]


class FunctionActionProvider(ActionProvider):
    """An action provider that wraps a plain callable.

    Use this instead of writing a one-off ``ActionProvider`` class
    when the available actions depend on the current state or goal.
    The planner calls the function on every search step, so it
    should be cheap and should not rely on being called only once.

    The function receives exactly the arguments the protocol
    receives: ``(state, goal)``. It may return a list of
    :class:`Action` objects, an :class:`Actions` collection, a
    single :class:`Action`, or ``None`` (meaning no actions).
    Anything else raises :class:`TypeError` loudly, matching the
    planner's rule that broken providers fail instead of being
    silently skipped. Exceptions raised by the function itself
    also propagate to the caller.
    """

    def __init__(
        self,
        func: ActionFunction,
        name: str | None = None,
    ) -> None:
        if not callable(func):
            raise TypeError("func must be callable")
        self.func = func
        self.name = name or getattr(func, "__name__", "function_provider")

    def provide_actions(
        self, state: WorldState, goal: Goal | dict[str, Any] | None = None
    ) -> list[Action]:
        result = self.func(state, goal)
        if result is None:
            return []
        if isinstance(result, Actions):
            return result.get_actions()
        if isinstance(result, Action):
            return [result]
        if isinstance(result, (list, tuple)):
            for item in result:
                if not isinstance(item, Action):
                    raise TypeError(
                        "FunctionActionProvider function must return "
                        "Action objects, got "
                        f"{type(item).__name__!r}"
                    )
            return list(result)
        raise TypeError(
            "FunctionActionProvider function must return a list of "
            "Action objects, an Actions collection, a single Action, "
            f"or None, got {type(result).__name__!r}"
        )

    def __repr__(self) -> str:
        return f"FunctionActionProvider(name={self.name!r})"


class StaticActionProvider(ActionProvider):
    """An action provider that wraps a static collection of actions."""

    def __init__(self, actions: Actions) -> None:
        self.actions = actions

    def provide_actions(
        self, state: WorldState, goal: Goal | dict[str, Any] | None = None
    ) -> list[Action]:
        return self.actions.get_actions()
