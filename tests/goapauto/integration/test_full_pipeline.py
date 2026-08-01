"""End-to-end integration tests exercising the full GOAP pipeline.

These tests wire together WorldState, Action, Goal, Sensors, GoalArbitrator,
Planner, and SearchTreeVisualizer to validate realistic agent scenarios.
"""

import pytest

from goapauto import (
    Action,
    Goal,
    GoalArbitrator,
    Increment,
    Planner,
    SearchTreeVisualizer,
    Sensor,
    SensorManager,
    WorldState,
)


class GoldSensor(Sensor):
    """Perceives available gold in the environment."""

    def sense(self) -> dict:
        return {"gold": 10}


class FullPipelineTest:
    """A complete agent loop: sense -> arbitrate -> plan -> visualize."""

    def _build_crafting_setup(self):
        """Build actions/goals for a gathering-and-crafting agent."""
        actions = [
            Action(
                name="gather_wood",
                preconditions={"has_axe": True},
                effects={"wood": Increment(5)},
                cost=2.0,
            ),
            Action(
                name="craft_axe",
                preconditions={"has_stone": True},
                effects={"has_axe": True},
                cost=3.0,
            ),
            Action(
                name="get_stone",
                preconditions={"gold": lambda g: g >= 5},
                effects={"has_stone": True, "gold": Increment(-5)},
                cost=1.0,
            ),
            Action(
                name="build_shelter",
                preconditions={"wood": lambda w: w >= 5},
                effects={"shelter_built": True},
                cost=4.0,
            ),
        ]
        return actions

    def test_full_agent_loop(self):
        """Sense, arbitrate, plan, and visualize in one continuous flow."""
        # 1. Sense the environment
        state = WorldState(gold=0, has_stone=False, has_axe=False, wood=0)
        sensor_manager = SensorManager(sensors=[GoldSensor()])
        sensor_manager.update_state(state)
        assert state.gold == 10

        # 2. Arbitrate between competing goals
        shelter_goal = Goal(
            target_state={"shelter_built": True}, priority=1, name="Build Shelter"
        )
        lumber_goal = Goal(
            target_state={"wood": 100}, priority=2, name="Stockpile Wood"
        )
        arbitrator = GoalArbitrator(goals=[shelter_goal, lumber_goal])
        selected = arbitrator.select_goal(state)
        assert selected.name == "Build Shelter"

        # 3. Plan to achieve the selected goal
        planner = Planner(actions_list=self._build_crafting_setup())
        result = planner.generate_plan(state, selected)

        assert result.plan is not None
        # gold >= 5 -> get_stone -> craft_axe -> gather_wood -> build_shelter
        assert result.plan[0] == "get_stone"
        assert result.plan[-1] == "build_shelter"

        # 4. Visualize the search tree
        visualizer = SearchTreeVisualizer()
        planner2 = Planner(actions_list=self._build_crafting_setup())
        planner2.register_hook("on_node_expanded", visualizer.on_node_expanded)
        planner2.generate_plan(state, selected)

        mermaid = visualizer.to_mermaid()
        assert "graph TD" in mermaid
        assert visualizer.nodes

    def test_temporal_planning_with_durations(self):
        """Plan with timed actions produces a correct schedule."""
        planner = Planner(
            actions_list=[
                ("mine", {"has_pickaxe": True}, {"ore": 1}, 2.0),
                ("smelt", {"ore": 1}, {"metal": 1}, 3.0),
            ]
        )
        for provider in planner.providers:
            for action in provider.actions.get_actions():
                action.duration = 2.0

        state = WorldState(has_pickaxe=True, ore=0, metal=0)
        goal = Goal(target_state={"metal": 1})

        result = planner.generate_plan(state, goal)

        assert result.plan == ["mine", "smelt"]
        assert result.schedule is not None
        steps = result.schedule.steps
        assert steps[0].action == "mine"
        assert steps[0].start_time == 0.0
        assert steps[0].end_time == 2.0
        assert steps[1].action == "smelt"
        assert steps[1].start_time == 2.0
        assert steps[1].end_time == 4.0
        assert result.schedule.makespan == 4.0
        assert result.schedule.total_cost == 5.0

    def test_replanning_after_execution(self):
        """Re-plan from a checkpoint after executing partial actions."""
        planner = Planner(
            actions_list=[
                ("step_a", {"start": True}, {"a_done": True}, 1.0),
                ("step_b", {"a_done": True}, {"b_done": True}, 1.0),
                ("step_c", {"b_done": True}, {"goal_reached": True}, 1.0),
            ]
        )
        state = WorldState(start=True, a_done=True, b_done=False, goal_reached=False)
        goal = Goal(target_state={"goal_reached": True})

        result = planner.continue_plan(state, goal, executed_actions=["step_a"])
        assert result.plan == ["step_b", "step_c"]


@pytest.mark.asyncio
async def test_async_full_pipeline_entry():
    """Async entry point wired to the pipeline test."""
    state = WorldState(gold=0, has_stone=False)
    SensorManager(sensors=[GoldSensor()]).update_state(state)

    goal = Goal(target_state={"has_stone": True}, priority=1, name="Get Stone")
    planner = Planner(
        actions_list=[
            ("get_stone", {"gold": lambda g: g >= 5}, {"has_stone": True}, 1.0)
        ]
    )

    result = await planner.async_generate_plan(state, goal)
    assert result.plan == ["get_stone"]
