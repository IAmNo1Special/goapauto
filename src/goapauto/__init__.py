"""
GOAP (Goal-Oriented Action Planning) implementation for AI agents.

This package provides a flexible framework for creating goal-driven AI agents
using the GOAP (Goal-Oriented Action Planning) architecture.

Key Components:
    - Planner: The main planning engine that finds optimal action sequences
    - Action: Base class for defining actions with preconditions and effects
    - Goal: Represents objectives that the agent wants to achieve
    - WorldState: Tracks the current state of the world
    - Sensors: Perception system (SensorManager, Sensor)
    - Arbitration: Goal selection (GoalArbitrator)
    - Visualizer: Debugging tool (SearchTreeVisualizer)

Example usage:
    >>> from goapauto import Planner, Goal, WorldState, Action
    >>>
    >>> # Define initial state
    >>> state = WorldState(has_key=False, door_open=False)
    >>>
    >>> # Create actions
    >>> pickup_key = Action(
    ...     name="pickup_key",
    ...     preconditions={'key_available': True},
    ...     effects={'has_key': True},
    ...     cost=1.0
    ... )
    >>>
    >>> # Create goal
    >>> goal = Goal(target_state={'door_open': True})
    >>>
    >>> # Plan
    >>> planner = Planner(actions_list=[pickup_key])
    >>> result = planner.generate_plan(state, goal)
"""

from goapauto.models.actions import Action, Actions
from goapauto.models.goal import Goal
from goapauto.models.goal_arbitrator import GoalArbitrator
from goapauto.models.goap_planner import Plan, Planner, PlanResult, PlanStats
from goapauto.models.sensors import Sensor, SensorManager
from goapauto.models.worldstate import WorldState
from goapauto.utils.visualizer import SearchTreeVisualizer

__version__ = "0.2.0"
__all__ = [
    "Planner",
    "Goal",
    "Action",
    "Actions",
    "WorldState",
    "PlanResult",
    "PlanStats",
    "Plan",
    "Sensor",
    "SensorManager",
    "GoalArbitrator",
    "SearchTreeVisualizer",
]
