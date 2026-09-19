#!/usr/bin/env python3
"""
Dinner decision — Jev edition.

Same branching actions and visualizer as search_tree.py, but the *goal* is
judged from the evening, not hard-coded:

- JevSensor reads the situation (payday? date night? cash on hand?) and
  judges ``celebrating`` as a probability. That one number flips the whole
  evening: date night -> "Full and Happy", broke Tuesday -> "Just Get Full".
- JevGoalStrategy makes the call, the planner plans, the visualizer draws
  the tree for whichever goal won.

Run offline (canned judgments stand in for Jev):
    python search_tree_jev.py --demo
Run live (key from `.env` in the repo root, or `TYPESAFE_API_KEY` exported):
    python search_tree_jev.py
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
    SystemOneResponse,
    TypeSafeClient,
    Usage,
)
from goapauto.utils.visualizer import SearchTreeVisualizer  # noqa: E402

load_dotenv()  # repo-root .env -> os.environ (exported vars win)

logging.basicConfig(level=logging.WARNING)

EVENINGS = [
    {
        "label": "Friday, date night",
        "observation": {
            "day": "Friday",
            "payday_was": "yesterday",
            "cash": 60,
            "plans": "date night, wants to impress",
        },
        "celebrating": 0.93,
    },
    {
        "label": "Tuesday, broke",
        "observation": {
            "day": "Tuesday",
            "payday_was": "last week",
            "cash": 12,
            "plans": "nothing, couch",
        },
        "celebrating": 0.08,
    },
]

QUESTIONS = {
    "celebrating": Noul(
        instructions="Is tonight a special occasion worth spending money on?"
    ),
}


class DemoClient:
    """Offline stand-in returning SDK-shaped responses. Delete when live."""

    def __init__(self, evening: dict) -> None:
        self._evening = evening

    def system_one(self, state: Any, questions: dict) -> SystemOneResponse:
        usage = Usage(input_tokens=0, output_tokens=0)
        if "goal" in questions:  # goal-arbitration call
            celebrating = state.get("world_state", {}).get("celebrating", 0)
            pick = "Full and Happy" if celebrating > 0.5 else "Just Get Full"
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
        return SystemOneResponse(
            model="demo",
            usage=usage,
            answers={
                "celebrating": NoulAnswer(noul=self._evening["celebrating"]),
            },
        )


def get_actions():
    return [
        (
            "buy_regular_ingredients",
            {"at_home": True, "money": lambda m: m >= 5},
            {"has_ingredients": True, "money": lambda m: m - 5, "quality": "regular"},
            2.0,
        ),
        (
            "buy_fancy_ingredients",
            {"at_home": True, "money": lambda m: m >= 15},
            {"has_ingredients": True, "money": lambda m: m - 15, "quality": "fancy"},
            2.0,
        ),
        (
            "cook_regular_meal",
            {"has_ingredients": True, "quality": "regular"},
            {"is_hungry": False, "is_happy": False, "has_ingredients": False},
            3.0,
        ),
        (
            "cook_fancy_meal",
            {"has_ingredients": True, "quality": "fancy"},
            {"is_hungry": False, "is_happy": True, "has_ingredients": False},
            3.0,
        ),
        (
            "order_fast_food",
            {"money": lambda m: m >= 8},
            {"is_hungry": False, "money": lambda m: m - 8},
            1.0,
        ),
        (
            "go_to_restaurant",
            {"at_home": True},
            {"at_home": False, "at_restaurant": True},
            5.0,
        ),
        (
            "eat_fine_dining",
            {"at_restaurant": True, "money": lambda m: m >= 25},
            {"is_hungry": False, "is_happy": True, "money": lambda m: m - 25},
            2.0,
        ),
        (
            "do_freelance_gig",
            {"at_home": True},
            {"money": lambda m: m + 10},
            4.0,
        ),
    ]


def run_evening(evening: dict, demo: bool, idx: int) -> None:
    client = DemoClient(evening) if demo else TypeSafeClient()
    sensor = JevSensor(
        observe=lambda: evening["observation"],
        questions=QUESTIONS,
        client=client,  # type: ignore[arg-type]
    )
    sensors = SensorManager([sensor])

    arbitrator = GoalArbitrator(
        goals=[
            Goal(
                name="Full and Happy",
                target_state={"is_hungry": False, "is_happy": True},
                priority=2,
            ),
            Goal(
                name="Just Get Full",
                target_state={"is_hungry": False},
                priority=1,
            ),
        ],
        strategy=JevGoalStrategy(client=client),  # type: ignore[arg-type]
    )

    state = WorldState(
        at_home=True,
        at_restaurant=False,
        money=evening["observation"]["cash"],
        is_hungry=True,
        is_happy=False,
        has_ingredients=False,
        quality="none",
        celebrating=0.0,
    )
    sensors.update_state(state)
    print(f"--- {evening['label']}: celebrating={state['celebrating']:.2f}")

    goal = arbitrator.select_goal(state)
    if goal is None:
        print("All goals satisfied.\n")
        return
    print(f"Jev chose goal: {goal.name}")

    planner = Planner(actions_list=get_actions())
    viz = SearchTreeVisualizer()
    planner.register_hook("on_node_expanded", viz.on_node_expanded)
    result = planner.generate_plan(state, goal)

    if result.plan:
        print("Plan:")
        for i, step in enumerate(result.plan, 1):
            print(f"  {i}. {step}")
        out = f"search_tree_jev_{idx}.md"
        viz.export(out)
        print(f"Tree exported to {out}")
    else:
        print(f"Plan failed: {result.message}")
    print()


def main() -> int:
    demo = "--demo" in sys.argv
    if demo:
        print("(demo mode: canned judgments, no API key needed)\n")
    for idx, evening in enumerate(EVENINGS):
        run_evening(evening, demo, idx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
