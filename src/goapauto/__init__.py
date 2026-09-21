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

from typing import Any

from goapauto.models.action_provider import ActionProvider, StaticActionProvider
from goapauto.models.actions import (
    Action,
    Actions,
    Decrement,
    Delete,
    Equal,
    GreaterThan,
    Increment,
    LessThan,
    NotEqual,
    Range,
    Set,
    Unset,
)
from goapauto.models.caching import (
    AsyncSensor,
    CachePolicy,
    CachingSensor,
    SensorHealth,
    SensorReport,
    Staleness,
    StalePolicy,
    ValueMeta,
    max_age,
    oldest_staleness,
)
from goapauto.models.execution import (
    InterruptSource,
    PlanExecution,
    PlanInterruptedError,
)
from goapauto.models.goal import Goal
from goapauto.models.goal_arbitrator import (
    GoalArbitrator,
    GoalSelectionStrategy,
    PriorityGoalStrategy,
)
from goapauto.models.goap_planner import (
    Plan,
    PlanExecutionError,
    Planner,
    PlanResult,
    PlanStats,
    Schedule,
    ScheduleStep,
)
from goapauto.models.judgment import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    FloatAnswer,
    Judge,
    JudgmentCallRecord,
    JudgmentError,
    JudgmentGoalStrategy,
    JudgmentResponse,
    JudgmentSensor,
    JudgmentStats,
    NoulQuestion,
    Question,
    ScoreQuestion,
    TokenUsage,
)
from goapauto.models.replan import ReplanDecision, ReplanPolicy, ReplanReason
from goapauto.models.sensors import Sensor, SensorManager
from goapauto.models.worldstate import WorldState
from goapauto.utils.inspector import AgentInspector, EventType, TraceEvent
from goapauto.utils.visualizer import SearchTreeVisualizer

__version__ = "0.6.1"

# Jev symbols are lazy: typesafe-sdk is an optional extra ("goapauto[jev]"),
# so importing them must not fail at package import time. PEP 562.
_JEV_ATTRS = frozenset(
    {
        "JevSensor",
        "JevGoalStrategy",
        "JevJudge",
        "JevClient",
        "JevCallRecord",
        "JevStats",
        "GateDecision",
        "shared_client",
        "TypeSafeClient",
        "TypeSafeError",
    }
)


def __getattr__(name: str) -> Any:
    if name in _JEV_ATTRS:
        from goapauto.models import jev

        return getattr(jev, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Planner",
    "Goal",
    "Action",
    "Actions",
    "WorldState",
    "PlanResult",
    "PlanStats",
    "PlanExecutionError",
    "PlanExecution",
    "PlanInterruptedError",
    "InterruptSource",
    "Plan",
    "Schedule",
    "ScheduleStep",
    "Sensor",
    "SensorManager",
    "GoalArbitrator",
    "GoalSelectionStrategy",
    "PriorityGoalStrategy",
    "ActionProvider",
    "StaticActionProvider",
    "NoulQuestion",
    "ChoiceQuestion",
    "ScoreQuestion",
    "Question",
    "FloatAnswer",
    "ChoiceAnswer",
    "Answer",
    "TokenUsage",
    "JudgmentResponse",
    "Judge",
    "JudgmentError",
    "JudgmentCallRecord",
    "JudgmentStats",
    "JudgmentSensor",
    "JudgmentGoalStrategy",
    "CachePolicy",
    "CachingSensor",
    "StalePolicy",
    "Staleness",
    "ValueMeta",
    "SensorReport",
    "SensorHealth",
    "AsyncSensor",
    "max_age",
    "oldest_staleness",
    "ReplanPolicy",
    "ReplanDecision",
    "ReplanReason",
    "JevSensor",
    "JevGoalStrategy",
    "JevJudge",
    "JevClient",
    "JevCallRecord",
    "JevStats",
    "GateDecision",
    "shared_client",
    "TypeSafeClient",
    "TypeSafeError",
    "AgentInspector",
    "TraceEvent",
    "EventType",
    "SearchTreeVisualizer",
    "Unset",
    "Delete",
    "Set",
    "Increment",
    "Decrement",
    "Equal",
    "NotEqual",
    "GreaterThan",
    "LessThan",
    "Range",
]
