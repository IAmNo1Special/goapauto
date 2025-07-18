"""
GOAP (Goal-Oriented Action Planning) implementation for AI agents.

This package provides a flexible framework for creating goal-driven AI agents
using the GOAP (Goal-Oriented Action Planning) architecture.

Key Components:
    - Planner: The main planning engine that finds optimal action sequences
    - Action: Base class for defining actions with preconditions and effects
    - Actions: Collection of available actions
    - Goal: Represents objectives that the agent wants to achieve
    - WorldState: Tracks the current state of the world
    - PlanResult: Type alias for the planning result (plan, status) tuple
    - PlanStats: Statistics about the planning process
    - Plan: Type alias for a list of action names

Example usage:
    >>> from goapauto import Planner, Goal, WorldState, Actions
    >>>
    >>> # Define initial state
    >>> world_state = WorldState({'has_key': False, 'door_open': False})
    >>>
    >>> # Create actions
    >>> actions = Actions()
    >>> actions.add_action(
    ...     name="pickup_key",
    ...     preconditions={'key_available': True},
    ...     effects={'has_key': True},
    ...     cost=1.0
    ... )
    >>>
    >>> # Create goal
    >>> goal = Goal("OpenDoor", 1, {'door_open': True})
    >>>
    >>> # Plan and execute
    >>> planner = Planner(actions)
    >>> plan, status = planner.generate_plan(world_state, goal)
"""

from .models.goap_planner import Planner, PlanResult, PlanStats, Plan
from .models.goal import Goal
from .models.actions import Action, Actions
from .models.worldstate import WorldState

__version__ = "0.1.1"
__all__ = [
    'Planner',
    'Goal',
    'Action',
    'Actions',
    'WorldState',
    'PlanResult',
    'PlanStats',
    'Plan'
]