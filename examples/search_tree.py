#!/usr/bin/env python3
"""
Complex Search Tree Demo

This example demonstrates a more complex planning scenario that results in a
branching A* search tree, showcasing the visualizer's capabilities.
"""

import logging
import sys
from pathlib import Path

# Add the parent directory to the Python path
sys.path.append(str(Path(__file__).parent.parent))

from goapauto import Goal, Planner, WorldState
from goapauto.utils.visualizer import SearchTreeVisualizer

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(message)s")


def run_complex_demo():
    print("==================================================")
    print("GOAP COMPLEX SEARCH TREE DEMO")
    print("==================================================")

    # 1. Define the initial state (Agent is at home with some money, hungry and unhappy)
    initial_state = WorldState(
        at_home=True,
        at_restaurant=False,
        money=20,
        is_hungry=True,
        is_happy=False,
        has_ingredients=False,
        quality="none",
    )

    # 2. Define the goal: Be full AND happy
    goal = Goal(
        name="Be Full and Happy", target_state={"is_hungry": False, "is_happy": True}
    )

    # 3. Define the available actions with branching possibilities
    actions_list = [
        # Path 1: Cook at home (Branching: Regular vs Fancy)
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
        # Path 2: Fast Food (Cheap, but doesn't make happy)
        (
            "order_fast_food",
            {"money": lambda m: m >= 8},
            {"is_hungry": False, "money": lambda m: m - 8},
            1.0,
        ),
        # Path 3: Fine Dining (Expensive, but makes happy)
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
        # Supplemental: Work to get more money
        (
            "do_freelance_gig",
            {"at_home": True},
            {"money": lambda m: m + 10},
            4.0,
        ),
    ]

    # 4. Initialize the Planner and Visualizer
    planner = Planner(actions_list=actions_list)
    viz = SearchTreeVisualizer()

    # 5. Register the visualizer hook
    planner.register_hook("on_node_expanded", viz.on_node_expanded)

    print("\nGenerating plan for 'Full and Happy'...")
    result = planner.generate_plan(initial_state, goal)

    if result.plan:
        print(f"\n[SUCCESS] Plan found with {len(result.plan)} steps:")
        for i, step in enumerate(result.plan, 1):
            print(f"  {i}. {step}")

        # 6. Export the tree
        output_file = "search_tree.md"
        viz.export(output_file)

        print("\n==================================================")
        print("VISUALIZATION GENERATED")
        print("==================================================")
        print(f"1. A Mermaid diagram has been saved to: {output_file}")
        print("2. This tree includes multiple exploration paths.")
        print("==================================================")
    else:
        print(f"\n[FAILURE] {result.message}")
        # Still export the tree to see what was explored
        viz.export("failed_search_tree.md")


if __name__ == "__main__":
    run_complex_demo()
