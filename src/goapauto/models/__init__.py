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
from goapauto.models.node import Node
from goapauto.models.sensors import Sensor, SensorManager
from goapauto.models.worldstate import WorldState

# Jev symbols are lazy: typesafe-sdk is an optional extra ("goapauto[jev]"),
# so importing this package must not fail when it is missing. PEP 562.
_JEV_ATTRS = frozenset(
    {"JevSensor", "JevGoalStrategy", "JevJudge", "TypeSafeClient", "TypeSafeError"}
)


def __getattr__(name: str) -> Any:
    if name in _JEV_ATTRS:
        from goapauto.models import jev

        return getattr(jev, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Action",
    "Actions",
    "ActionProvider",
    "StaticActionProvider",
    "Goal",
    "GoalArbitrator",
    "GoalSelectionStrategy",
    "PriorityGoalStrategy",
    "Planner",
    "PlanResult",
    "PlanStats",
    "PlanExecutionError",
    "Plan",
    "Schedule",
    "ScheduleStep",
    "Node",
    "WorldState",
    "Sensor",
    "SensorManager",
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
    "JevSensor",
    "JevGoalStrategy",
    "JevJudge",
    "TypeSafeClient",
    "TypeSafeError",
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
