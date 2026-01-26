import asyncio
from typing import Any, Dict

from goapauto.models.actions import Increment
from goapauto.models.goal import Goal
from goapauto.models.goal_arbitrator import GoalArbitrator
from goapauto.models.goap_planner import Planner
from goapauto.models.sensors import Sensor, SensorManager
from goapauto.models.worldstate import WorldState
from goapauto.utils.visualizer import SearchTreeVisualizer


# 1. Mock Sensor
class WoodSensor(Sensor):
    def sense(self) -> Dict[str, Any]:
        return {"wood": 10}  # Perceived 10 wood in environment


async def test_phase3():
    print("\n" + "=" * 50)
    print("TESTING PHASE 3: TOOLING & ADVANCED FEATURES")
    print("=" * 50)

    # --- Test 1: Sensors ---
    print("\n[TEST 1] Testing Sensors...")
    state = WorldState(wood=0)
    manager = SensorManager(sensors=[WoodSensor()])
    manager.update_state(state=state)
    print(f"Updated state after sensing: wood={state.wood}")
    assert state.wood == 10

    # --- Test 2: Goal Arbitrator ---
    print("\n[TEST 2] Testing Goal Arbitrator...")
    goal_low = Goal(target_state={"hunger": 0}, priority=10, name="Eat")
    goal_high = Goal(target_state={"escape": True}, priority=1, name="Escape")

    arbitrator = GoalArbitrator(goals=[goal_low, goal_high])
    state_arb = WorldState(hunger=5, escape=False)

    selected = arbitrator.select_goal(state=state_arb)
    print(f"Goal Arbitrator selected: {selected.name}")
    assert selected.name == "Escape"

    # --- Test 3: Visualization ---
    print("\n[TEST 3] Testing Search Tree Visualization (Mermaid)...")
    visualizer = SearchTreeVisualizer()
    planner = Planner(
        actions_list=[("increment_val", {}, {"val": Increment(amount=1)}, 1.0)]
    )
    planner.register_hook(
        event="on_node_expanded", callback=visualizer.on_node_expanded
    )

    state_vis = WorldState(val=0)
    goal_vis = Goal(target_state={"val": 2})

    await planner.async_generate_plan(world_state=state_vis, goal=goal_vis)

    mermaid_str = visualizer.to_mermaid()
    print("Generated Mermaid Diagram Snippet:")
    print("\n".join(mermaid_str.split("\n")[:10]))  # Show first 10 lines

    assert "graph TD" in mermaid_str
    assert "increment_val" in mermaid_str

    print("\n" + "=" * 50)
    print("PHASE 3 TESTS PASSED!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(test_phase3())
