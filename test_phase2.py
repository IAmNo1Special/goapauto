import asyncio
from typing import Any, Dict, List, Union

from goapauto.models.action_provider import ActionProvider
from goapauto.models.actions import Action, Increment
from goapauto.models.goal import Goal
from goapauto.models.goap_planner import Planner
from goapauto.models.worldstate import WorldState


# 1. ActionProvider Implementation
class DynamicWoodProvider(ActionProvider):
    def provide_actions(self, state: WorldState) -> List[Action]:
        # Only provide "gather" if wood is less than 3
        if getattr(state, "wood", 0) < 3:
            return [Action("gather_wood", {}, {"wood": Increment(amount=1)})]
        return []


# 2. Custom Heuristic Function
def coord_heuristic(state: WorldState, goal: Union[Goal, Dict[str, Any]]) -> float:
    # Manhattan distance for a simple 'pos' coordinate
    if hasattr(goal, "target_state"):
        target = goal.target_state.get("pos", 0)
    else:
        target = goal.get("pos", 0)

    current = getattr(state, "pos", 0)
    return float(abs(target - current))


async def test_extensibility():
    print("\n" + "=" * 50)
    print("TESTING PHASE 2: EXTENSIBILITY")
    print("=" * 50)

    # --- Test 1: ActionProvider ---
    print("\n[TEST 1] Testing ActionProvider...")
    initial_state = WorldState(wood=0)
    goal = {"wood": 3}

    planner = Planner(providers=[DynamicWoodProvider()])
    plan, message = await planner.async_generate_plan(initial_state, goal)
    print(f"Result: {message}")
    if plan:
        print(f"Plan: {plan}")
    assert plan is not None
    assert len(plan) == 3

    # --- Test 2: Middleware Hooks ---
    print("\n[TEST 2] Testing Middleware Hooks (on_node_expanded)...")
    expansion_count = 0

    def on_expand(node):
        nonlocal expansion_count
        expansion_count += 1

    planner.register_hook("on_node_expanded", on_expand)
    await planner.async_generate_plan(initial_state, goal)
    print(f"Hook counter: {expansion_count} nodes expanded")
    assert expansion_count > 0

    # --- Test 3: Custom Heuristic ---
    print("\n[TEST 3] Testing Custom Heuristic (Coordinate Distance)...")
    state_coord = WorldState(pos=0)
    goal_coord = {"pos": 5}

    # Setup planner with static movement action
    planner_coord = Planner(
        actions_list=[("move_right", {}, {"pos": Increment(amount=1)}, 1.0)]
    )

    # Run with custom heuristic
    plan_h, msg_h = await planner_coord.async_generate_plan(
        state_coord, goal_coord, heuristic_fn=coord_heuristic
    )
    print(f"Result: {msg_h}")
    if plan_h:
        print(f"Plan: {plan_h}")
    assert plan_h is not None
    assert len(plan_h) == 5

    print("\n" + "=" * 50)
    print("PHASE 2 TESTS PASSED!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(test_extensibility())
if __name__ == "__main__":
    asyncio.run(test_extensibility())
