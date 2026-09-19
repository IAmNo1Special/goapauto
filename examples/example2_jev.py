#!/usr/bin/env python3
"""
Morning routine — Jev edition.

Same actions as example2.py, but the *goal* is judged, not hard-coded:

- JevSensor reads the morning (clock, snoozes, sleep) and judges
  ``running_late`` as a probability and ``grogginess`` on a rubric.
- JevGoalStrategy picks between "Full Routine" and "Rushed Exit" from the
  live state. Same script, different morning, different goal, different plan.

Run offline (canned judgments stand in for Jev):
    python example2_jev.py --demo
Run live (key from `.env` in the repo root, or `TYPESAFE_API_KEY` exported):
    python example2_jev.py
"""

import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _env import load_dotenv  # noqa: E402

from goapauto import (  # noqa: E402
    Goal,
    GoalArbitrator,
    JevGoalStrategy,
    JevSensor,
    Planner,
    SensorManager,
    WorldState,
)
from typesafe_sdk import (  # noqa: E402
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    SystemOneResponse,
    TypeSafeClient,
    Usage,
)

load_dotenv()  # repo-root .env -> os.environ (exported vars win)

logging.basicConfig(level=logging.WARNING)

MORNINGS = [
    {
        "label": "Monday, alarm snoozed 3x",
        "observation": {
            "time": "08:41",
            "first_meeting": "09:00",
            "alarm_snoozes": 3,
            "sleep_hours": 5.5,
        },
        "running_late": 0.88,
        "grogginess": 2.0,  # rubric: 0 fresh, 1 groggy, 2 zombie
    },
    {
        "label": "Sunday, no alarm",
        "observation": {
            "time": "09:15",
            "first_meeting": "none",
            "alarm_snoozes": 0,
            "sleep_hours": 8.0,
        },
        "running_late": 0.04,
        "grogginess": 0.0,
    },
]

QUESTIONS = {
    "running_late": Noul(
        instructions="Is the person running late for their first commitment?"
    ),
    "grogginess": Score(
        instructions="How groggy is the person?",
        criteria=["fresh", "groggy", "zombie"],
    ),
}


class DemoClient:
    """Offline stand-in returning SDK-shaped responses. Delete when live."""

    def __init__(self, morning: dict) -> None:
        self._morning = morning

    def system_one(self, state: Any, questions: dict) -> SystemOneResponse:
        usage = Usage(input_tokens=0, output_tokens=0)
        if "goal" in questions:  # goal-arbitration call
            late = state.get("world_state", {}).get("running_late", 0)
            pick = "Rushed Exit" if late > 0.6 else "Full Routine"
            return SystemOneResponse(
                model="demo",
                usage=usage,
                answers={
                    "goal": ChoiceAnswer(
                        choice=pick,
                        confidence=1.0,
                        probabilities={pick: 1.0},
                    )
                },
            )
        grogginess = float(self._morning["grogginess"])
        return SystemOneResponse(
            model="demo",
            usage=usage,
            answers={
                "running_late": NoulAnswer(noul=self._morning["running_late"]),
                "grogginess": ScoreAnswer(
                    score=grogginess,
                    confidence=1.0,
                    legend={0: "fresh", 1: "groggy", 2: "zombie"},
                    probabilities={int(grogginess): 1.0},
                ),
            },
        )


def get_actions():
    return [
        ("wake_up", {"awake": False}, {"awake": True}, 1.0),
        ("shower", {"awake": True, "showered": False}, {"showered": True}, 10.0),
        ("get_dressed", {"awake": True, "dressed": False}, {"dressed": True}, 5.0),
        (
            "make_coffee",
            {"awake": True, "coffee_made": False},
            {"coffee_made": True},
            5.0,
        ),
        (
            "eat_breakfast",
            {"awake": True, "breakfast_eaten": False},
            {"breakfast_eaten": True},
            10.0,
        ),
    ]


def run_morning(morning: dict, demo: bool) -> None:
    client = DemoClient(morning) if demo else TypeSafeClient()
    sensor = JevSensor(
        observe=lambda: morning["observation"],
        questions=QUESTIONS,
        client=client,  # type: ignore[arg-type]
    )
    sensors = SensorManager([sensor])

    arbitrator = GoalArbitrator(
        goals=[
            Goal(
                name="Full Routine",
                target_state={
                    "showered": True,
                    "dressed": True,
                    "coffee_made": True,
                    "breakfast_eaten": True,
                },
                priority=2,
            ),
            Goal(
                name="Rushed Exit",
                target_state={"awake": True, "dressed": True},
                priority=1,
            ),
        ],
        strategy=JevGoalStrategy(client=client),  # type: ignore[arg-type]
    )
    planner = Planner(actions_list=get_actions())

    state = WorldState(
        awake=False,
        showered=False,
        dressed=False,
        coffee_made=False,
        breakfast_eaten=False,
        running_late=0.0,
        grogginess=0.0,
    )
    sensors.update_state(state)
    print(
        f"--- {morning['label']}: "
        f"running_late={state['running_late']:.2f} grogginess={state['grogginess']:.1f}"
    )

    goal = arbitrator.select_goal(state)
    if goal is None:
        print("All goals satisfied.\n")
        return
    print(f"Jev chose goal: {goal.name}")
    result = planner.generate_plan(world_state=state, goal=goal)
    if result.plan:
        for action_name in result.plan:
            print(f"  - {action_name}")
    else:
        print(f"  plan failed: {result.message}")
    print()


def main() -> int:
    demo = "--demo" in sys.argv
    if demo:
        print("(demo mode: canned judgments, no API key needed)\n")
    for morning in MORNINGS:
        run_morning(morning, demo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
