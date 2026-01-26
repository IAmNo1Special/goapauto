#!/usr/bin/env python3
"""
Morning Routine Planner

This script demonstrates using the GOAP (Goal-Oriented Action Planning) system
for planning an optimal morning routine based on different constraints and goals.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Add the parent directory to the Python path
sys.path.append(str(Path(__file__).parent.parent))

from goapauto import Goal, Planner, WorldState

# Configure logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


def get_initial_state() -> WorldState:
    """Create and return the initial morning state."""
    return WorldState(
        awake=False,
        showered=False,
        dressed=False,
        coffee_made=False,
        breakfast_eaten=False,
    )


def get_actions_list() -> List[Tuple[str, Dict[str, Any], Dict[str, Any], float]]:
    """Define the available routine actions."""
    from goapauto.models.actions import Increment, Set

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


def main() -> int:
    """Main function demonstrating the modernized GOAP system."""
    try:
        initial_state = get_initial_state()
        actions = get_actions_list()

        goal = Goal(
            target_state={"awake": True, "showered": True, "dressed": True},
            priority=1,
            name="Ready for Work",
        )

        planner = Planner(actions_list=actions)
        plan_result = planner.generate_plan(world_state=initial_state, goal=goal)

        print(f"\nResult: {plan_result.message}")
        if plan_result.plan:
            print("Optimal Morning Routine:")
            for action_name in plan_result.plan:
                print(f"- {action_name}")
        return 0

    except Exception as e:
        logger.exception("Planning failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
