"""
This script demonstrates the GOAP (Goal-Oriented Action Planning) system
for the GuwopMon game bot. It defines the game state, available actions,
and demonstrates how to generate and execute plans to achieve goals.
"""

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Add the parent directory to the Python path
sys.path.append(str(Path(__file__).parent.parent))

from goapauto import Goal, Planner, PlanResult, WorldState

# Configure logging - only show WARNING and above
logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def get_initial_state() -> WorldState:
    """Create and return the initial game state.

    Returns:
        Dictionary representing the initial state of the game.
    """
    return WorldState(
        is_open=False,  # Game client window is closed
        is_focused=False,  # Game client doesn't have focus
        is_loading=False,  # Game is not currently loading
        guwopmon_app_open=False,  # GuwopMon app is not open
        guwopmon_game_started=False,  # GuwopMon game is not started
        guwopmon_game_loading=False,  # GuwopMon game is not loading
        logged_in=False,  # User is not logged in
        logging_in=False,  # Login process is not in progress
        menu_open=False,  # In-game menu is closed
        tv_open=False,  # TV interface is closed
        mon_selected=False,  # No GuwopMon is selected
        location="outside",  # Current location in-game
        is_grading=False,  # Not currently grading GuwopMon
        mons_graded=False,  # No GuwopMon has been graded yet
    )


def get_actions_list() -> List[Tuple[str, Dict[str, Any], Dict[str, Any], float]]:
    """Define the list of available actions with their preconditions and effects.

    Returns:
        List of action tuples (name, preconditions, effects, cost)
    """
    return [
        # Client management actions
        (
            "open_client",
            {"is_open": False, "is_loading": False},
            {"is_open": True},
            1.0,
        ),
        (
            "focus_game_client",
            {"is_open": True, "is_focused": False},
            {"is_focused": True},
            1.0,
        ),
        # Game launch sequence
        (
            "open_guwopmon_app",
            {"is_open": True, "guwopmon_app_open": False},
            {"guwopmon_app_open": True},
            1.0,
        ),
        (
            "start_game",
            {
                "is_open": True,
                "guwopmon_app_open": True,
                "guwopmon_game_started": False,
                "guwopmon_game_loading": False,
            },
            {"guwopmon_game_started": True},
            1.0,
        ),
        # Authentication
        (
            "log_in",
            {"guwopmon_game_started": True, "logged_in": False, "logging_in": False},
            {"logged_in": True},
            1.0,
        ),
        # Navigation
        (
            "enter_shop",
            {"logged_in": True, "menu_open": False, "location": "outside"},
            {"location": "revocenter"},
            1.0,
        ),
        (
            "leave_shop",
            {"logged_in": True, "menu_open": False, "location": "revocenter"},
            {"location": "outside"},
            1.0,
        ),
        # Menu interactions
        (
            "open_menu",
            {"logged_in": True, "menu_open": False},
            {"menu_open": True},
            1.0,
        ),
        ("close_menu", {"menu_open": True}, {"menu_open": False}, 1.0),
        # TV and grading system
        (
            "open_tv",
            {
                "logged_in": True,
                "menu_open": False,
                "tv_open": False,
                "location": "revocenter",
            },
            {"tv_open": True},
            1.0,
        ),
        ("close_tv", {"tv_open": True}, {"tv_open": False}, 1.0),
        ("select_mon", {"tv_open": True}, {"mon_selected": True}, 1.0),
        (
            "grade_guwopmon",
            {"tv_open": True, "mon_selected": True, "is_grading": False},
            {"mons_graded": True},
            1.0,
        ),
    ]


def main() -> None:
    """Main function to demonstrate the GOAP planner."""
    try:
        # Initialize state
        initial_state = get_initial_state()

        # Initialize actions
        actions_list = get_actions_list()

        # Define the goal
        goal = Goal(target_state={"tv_open": True}, name="Open in-game tv", priority=1)

        # Create and configure the planner
        planner = Planner(actions_list, max_iterations=5000)

        # Generate the plan
        result = planner.generate_plan(world_state=initial_state, goal=goal)

        if result.plan:
            logger.info("Plan generated successfully:")
            for action_name in result.plan:
                logger.info(f"- {action_name}")
        else:
            logger.warning("Plan generation failed:")
            logger.warning(f"Status: {result.message}")

        return 0

    except Exception as e:
        logger.exception(f"An error occurred during planning: {e}")
        return 1


if __name__ == "__main__":
    main()
