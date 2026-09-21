#!/usr/bin/env python3
"""Jev reference loop — the canonical way to wire judgments into GOAP.

One batched sensor call judges the situation, JevGoalStrategy picks the
goal, the planner builds the plan. Jev does semantics; code does the rest.

Run offline (canned judgments, no API key):
    python jev_reference.py --demo
Run live (key from `.env` in the repo root, or `TYPESAFE_API_KEY` exported):
    python jev_reference.py
"""

import logging
import sys
from pathlib import Path

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
    shared_client,
)
from goapauto.testing import FakeTypeSafeClient  # noqa: E402
from typesafe_sdk import Noul, Score  # noqa: E402

load_dotenv()  # repo-root .env -> os.environ (exported vars win)

logging.basicConfig(level=logging.WARNING)

QUESTIONS = {
    "running_late": Noul(instructions="Is the person running late?"),
    "grogginess": Score(
        instructions="How groggy is the person?",
        criteria=["fresh", "groggy", "zombie"],
    ),
}

# Two mornings: one relaxed, one rushed. The canned judgments flip the goal.
MORNINGS = [
    {
        "label": "relaxed",
        "observation": {"alarm_snoozes": 0, "clock": "6:55am, plenty of time"},
        "answers": {
            "running_late": 0.1,
            "grogginess": 0.0,
            "goal": "Full Routine",
        },
    },
    {
        "label": "rushed",
        "observation": {"alarm_snoozes": 4, "clock": "8:40am, meeting at 9"},
        "answers": {
            "running_late": 0.95,
            "grogginess": 2.0,
            "goal": "Rushed Exit",
        },
    },
]


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


def run_morning(morning: dict, client) -> None:
    telemetry_log = []
    sensor = JevSensor(
        observe=lambda: morning["observation"],
        questions=QUESTIONS,
        client=client,
        telemetry=telemetry_log.append,
    )
    strategy = JevGoalStrategy(client=client, telemetry=telemetry_log.append)
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
        strategy=strategy,
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
    SensorManager(sensors=[sensor]).update_state(state)
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

    calls = len(telemetry_log)
    tokens = sum(r.input_tokens or 0 for r in telemetry_log)
    print(f"  ({calls} Jev call(s), ~{tokens} input tokens)\n")


def main() -> int:
    demo = "--demo" in sys.argv
    if demo:
        print("(demo mode: canned judgments, no API key needed)\n")
        for morning in MORNINGS:
            client = FakeTypeSafeClient(answers=morning["answers"])
            run_morning(morning, client)
    else:
        with shared_client() as client:  # one client for sensor + strategy
            for morning in MORNINGS:
                run_morning(morning, client)
    return 0


if __name__ == "__main__":
    sys.exit(main())
