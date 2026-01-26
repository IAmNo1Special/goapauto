from __future__ import annotations

import logging
from typing import List, Optional, Protocol, runtime_checkable

from goapauto.models.goal import Goal
from goapauto.models.worldstate import WorldState

logger = logging.getLogger(__name__)


@runtime_checkable
class GoalSelectionStrategy(Protocol):
    """Protocol for goal selection strategies."""

    def select(self, goals: List[Goal], state: WorldState) -> Optional[Goal]:
        """Select the best goal to pursue."""
        ...


class PriorityGoalStrategy:
    """Selects the goal with the highest priority (lowest number)."""

    def select(self, goals: List[Goal], state: WorldState) -> Optional[Goal]:
        if not goals:
            return None
        return min(goals, key=lambda g: g.priority)


class GoalArbitrator:
    """Manages multiple goals and selects the best one to pursue.

    The arbitrator uses a selection strategy to decide which goal the
    agent should focus on based on the current world state.
    """

    def __init__(
        self,
        goals: Optional[List[Goal]] = None,
        strategy: Optional[GoalSelectionStrategy] = None,
    ) -> None:
        self.goals = goals or []
        self.strategy = strategy or PriorityGoalStrategy()

    def add_goal(self, goal: Goal) -> None:
        """Add a goal to the arbitrator."""
        self.goals.append(goal)

    def remove_goal(self, name: str) -> None:
        """Remove a goal by name."""
        self.goals = [g for g in self.goals if g.name != name]

    def select_goal(self, state: WorldState) -> Optional[Goal]:
        """Select the best goal to pursue using the current strategy.

        Args:
            state: The current world state

        Returns:
            The selected Goal or None
        """
        # Filter out goals that are already satisfied
        active_goals = [g for g in self.goals if not g.is_satisfied(state)]

        if not active_goals:
            logger.info("All goals are already satisfied.")
            return None

        selected = self.strategy.select(active_goals, state)
        if selected:
            logger.info(
                "Selected goal: %s (Priority: %d)", selected.name, selected.priority
            )
        return selected
