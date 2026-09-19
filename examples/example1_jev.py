#!/usr/bin/env python3
"""
GuwopMon bot — Jev edition.

Same game and actions as example1.py, but perception and goal choice are
judgments now:

- JevSensor reads a screen description and judges state like ``is_open``,
  ``logged_in`` and ``stuck``. All 7 questions go out in ONE batched call,
  so judging the whole screen costs barely more than judging one thing.
- JevGoalStrategy picks between "Grade a GuwopMon" and "Recover session"
  from the live state, instead of the script hard-coding a single goal.

In a real bot, ``observe_screen()`` would return a scene description from
CV/OCR. Here it's simulated frames so you can see the loop react.

Run offline (canned judgments stand in for Jev):
    python example1_jev.py --demo
Run live (key from `.env` in the repo root, or `TYPESAFE_API_KEY` exported):
    python example1_jev.py
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

load_dotenv()  # repo-root .env -> os.environ (exported vars win)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


def yes(value: Any) -> bool:
    """Crisp check over a judged probability: >0.5 counts as true."""
    return value > 0.5


def no(value: Any) -> bool:
    return value <= 0.5


# Simulated screen descriptions, one per bot tick. A real bot builds these
# from CV/OCR; Jev does the "what am I looking at?" reasoning.
FRAMES = [
    {"screen": "Windows desktop. No game window visible."},
    {
        "screen": (
            "Game client window open on the login screen. "
            "A 'Connection failed' error dialog is showing."
        )
    },
    {
        "screen": (
            "Game client open. Character select screen, "
            "'Enter Revocenter' button visible. No error dialogs."
        )
    },
]

QUESTIONS = {
    "is_open": Noul(instructions="Is the game client window open?"),
    "game_started": Noul(instructions="Is the game past the launcher?"),
    "logged_in": Noul(instructions="Is the player logged in?"),
    "menu_open": Noul(instructions="Is the in-game menu open?"),
    "tv_open": Noul(instructions="Is the TV interface open?"),
    "mon_selected": Noul(instructions="Is a GuwopMon selected?"),
    "stuck": Noul(
        instructions="Is the client stuck: loading spinner, error dialog, "
        "or failed login?"
    ),
}

# Canned judgments for --demo mode: same shapes Jev returns, so the rest of
# the script is identical between demo and live.
DEMO_JUDGMENTS = [
    {
        "is_open": 0.03,
        "game_started": 0.02,
        "logged_in": 0.01,
        "menu_open": 0.0,
        "tv_open": 0.0,
        "mon_selected": 0.0,
        "stuck": 0.02,
    },
    {
        "is_open": 0.97,
        "game_started": 0.95,
        "logged_in": 0.04,
        "menu_open": 0.0,
        "tv_open": 0.0,
        "mon_selected": 0.0,
        "stuck": 0.92,
    },
    {
        "is_open": 0.98,
        "game_started": 0.97,
        "logged_in": 0.96,
        "menu_open": 0.0,
        "tv_open": 0.0,
        "mon_selected": 0.0,
        "stuck": 0.04,
    },
]


class DemoClient:
    """Offline stand-in returning SDK-shaped responses. Delete when live."""

    def system_one(self, state: Any, questions: dict) -> SystemOneResponse:
        if "goal" in questions:  # goal-arbitration call
            world = state.get("world_state", {})
            if world.get("stuck", 0) > 0.5 or world.get("logged_in", 0) <= 0.5:
                pick = "Recover session"
            else:
                pick = "Grade a GuwopMon"
            return SystemOneResponse(
                model="demo",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={
                    "goal": ChoiceAnswer(
                        choice=pick,
                        confidence=1.0,
                        probabilities={pick: 1.0},
                    )
                },
            )
        screen = state.get("screen", "")
        idx = next((i for i, f in enumerate(FRAMES) if f["screen"] == screen), 0)
        return SystemOneResponse(
            model="demo",
            usage=Usage(input_tokens=0, output_tokens=0),
            answers={
                name: NoulAnswer(noul=DEMO_JUDGMENTS[idx][name]) for name in questions
            },
        )


def get_actions():
    return [
        ("open_client", {"is_open": no}, {"is_open": 1.0}, 1.0),
        (
            "focus_game_client",
            {"is_open": yes, "is_focused": no},
            {"is_focused": 1.0},
            1.0,
        ),
        (
            "open_guwopmon_app",
            {"is_open": yes, "guwopmon_app_open": no},
            {"guwopmon_app_open": 1.0},
            1.0,
        ),
        (
            "start_game",
            {"is_open": yes, "guwopmon_app_open": yes, "game_started": no},
            {"game_started": 1.0},
            1.0,
        ),
        ("dismiss_error", {"stuck": yes}, {"stuck": 0.0}, 1.0),
        (
            "log_in",
            {"game_started": yes, "logged_in": no, "stuck": no},
            {"logged_in": 1.0},
            1.0,
        ),
        (
            "enter_shop",
            {"logged_in": yes, "location": "outside"},
            {"location": "revocenter"},
            1.0,
        ),
        (
            "open_tv",
            {"logged_in": yes, "location": "revocenter", "tv_open": no},
            {"tv_open": 1.0},
            1.0,
        ),
        (
            "select_mon",
            {"tv_open": yes, "mon_selected": no},
            {"mon_selected": 1.0},
            1.0,
        ),
        (
            "grade_guwopmon",
            {"tv_open": yes, "mon_selected": yes},
            {"mons_graded": True},
            1.0,
        ),
    ]


def main() -> int:
    demo = "--demo" in sys.argv
    client = DemoClient() if demo else TypeSafeClient()
    if demo:
        print("(demo mode: canned judgments, no API key needed)\n")

    frames = iter(FRAMES)
    sensor = JevSensor(
        observe=lambda: next(frames),
        questions=QUESTIONS,
        client=client,  # type: ignore[arg-type]
    )
    sensors = SensorManager([sensor])

    arbitrator = GoalArbitrator(
        goals=[
            Goal(
                name="Recover session",
                target_state={"logged_in": yes, "stuck": no},
                priority=1,
            ),
            Goal(
                name="Grade a GuwopMon",
                target_state={"mons_graded": True},
                priority=2,
            ),
        ],
        strategy=JevGoalStrategy(client=client),  # type: ignore[arg-type]
    )
    planner = Planner(actions_list=get_actions(), max_iterations=5000)

    for tick in range(len(FRAMES)):
        state = WorldState(
            is_open=0.0,
            is_focused=0.0,
            guwopmon_app_open=0.0,
            game_started=0.0,
            logged_in=0.0,
            stuck=0.0,
            menu_open=0.0,
            tv_open=0.0,
            mon_selected=0.0,
            mons_graded=False,
            location="outside",
        )
        sensors.update_state(state)
        print(
            f"--- tick {tick + 1}: stuck={state['stuck']:.2f} "
            f"logged_in={state['logged_in']:.2f}"
        )

        goal = arbitrator.select_goal(state)
        if goal is None:
            print("All goals satisfied.\n")
            continue
        print(f"Jev chose goal: {goal.name}")

        result = planner.generate_plan(world_state=state, goal=goal)
        if result.plan:
            for action_name in result.plan:
                print(f"  - {action_name}")
        else:
            print(f"  plan failed: {result.message}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
