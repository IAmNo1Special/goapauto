import asyncio
from typing import Any, Dict

import pytest

from goapauto.models.actions import Actions, Increment, Set
from goapauto.models.goal import Goal
from goapauto.models.goap_planner import Planner, PlanResult
from goapauto.models.worldstate import WorldState


class TestPlanner:
    def test_initialization(self, simple_actions):
        """Test planner initialization."""
        planner = Planner(actions_list=[])
        assert planner is not None
        assert planner.max_iterations == 1000

    def test_simple_plan_generation(self, simple_actions):
        """Test generating a basic plan."""
        planner = Planner(providers=[])
        # Manually add provider logic or use the helper constructor
        # Re-using the logic from the constructor for `actions_list` requires mocking or
        # using the public API. Let's use the public API with the actions list directly.

        # We need to extract tuples from our `simple_actions` fixture,
        # but `Actions` doesn't expose raw tuples easily.
        # Let's define a fresh list for clarity.
        actions_list = [
            ("step1", {"start": True}, {"mid": True}, 1.0),
            ("step2", {"mid": True}, {"end": True}, 1.0),
        ]

        planner = Planner(actions_list=actions_list)
        state = WorldState(start=True, mid=False, end=False)
        goal = Goal(target_state={"end": True})

        result = planner.generate_plan(state, goal)

        assert result.plan is not None
        assert result.plan == ["step1", "step2"]
        assert "SUCCESS" in result.message

    def test_plan_failure_unreachable(self):
        """Test planning when goal is unreachable."""
        planner = Planner(actions_list=[("step", {"a": 1}, {"b": 1}, 1.0)])
        state = WorldState(a=0)  # Condition not met
        goal = Goal(target_state={"c": 1})  # Goal unrelated

        result = planner.generate_plan(state, goal)
        assert result.plan is None
        assert "No valid plan" in result.message

    def test_goal_already_satisfied(self):
        """Test immediate return when goal is met."""
        planner = Planner(actions_list=[])
        state = WorldState(done=True)
        goal = Goal(target_state={"done": True})

        result = planner.generate_plan(state, goal)
        assert result.plan == []
        assert "already satisfied" in result.message

    @pytest.mark.asyncio
    async def test_async_plan_generation(self):
        """Test async planning capability."""
        actions_list = [("step", {"start": True}, {"end": True}, 1.0)]
        planner = Planner(actions_list=actions_list)
        state = WorldState(start=True)
        goal = Goal(target_state={"end": True})

        result = await planner.async_generate_plan(state, goal)
        assert result.plan == ["step"]

    def test_hooks(self, mocker):
        """Test that planner hooks are triggered."""
        mock_callback = mocker.Mock()
        actions_list = [("step", {"a": 1}, {"b": 1}, 1.0)]
        planner = Planner(actions_list=actions_list)
        planner.register_hook("on_node_expanded", mock_callback)

        state = WorldState(a=1)
        goal = Goal(target_state={"b": 1})

        planner.generate_plan(state, goal)

        # Verify hook was called
        assert mock_callback.call_count >= 1
