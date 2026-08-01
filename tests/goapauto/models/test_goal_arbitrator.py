from goapauto.models.goal import Goal
from goapauto.models.goal_arbitrator import (
    GoalArbitrator,
    PriorityGoalStrategy,
)
from goapauto.models.worldstate import WorldState


class TestGoalArbitrator:
    def test_priority_strategy(self):
        """Test selecting highest priority goal."""
        g_high = Goal(target_state={"a": 1}, priority=1, name="High")
        g_low = Goal(target_state={"b": 1}, priority=10, name="Low")

        arb = GoalArbitrator(goals=[g_low, g_high])
        state = WorldState(a=0, b=0)

        selected = arb.select_goal(state)
        assert selected.name == "High"

    def test_satisfied_glals_filtered(self):
        """Test that satisfied goals are ignored."""
        g_done = Goal(target_state={"done": True}, priority=1, name="Done")
        g_todo = Goal(target_state={"pending": True}, priority=2, name="Todo")

        arb = GoalArbitrator(goals=[g_done, g_todo])
        state = WorldState(done=True, pending=False)

        selected = arb.select_goal(state)
        assert (
            selected.name == "Todo"
        )  # Higher priority one satisfied, pick next available

    def test_no_applicable_goals(self):
        """Test return None when all goals satisfied."""
        g = Goal(target_state={"a": 1}, priority=1)
        state = WorldState(a=1)

        arb = GoalArbitrator(goals=[g])
        assert arb.select_goal(state) is None

    def test_goal_management(self):
        """Test adding/removing goals."""
        arb = GoalArbitrator()
        g = Goal(target_state={"a": 1}, priority=1, name="Dynamic")

        arb.add_goal(g)
        assert len(arb.goals) == 1

        arb.remove_goal("Dynamic")
        assert len(arb.goals) == 0

    def test_default_strategy(self):
        """Test GoalArbitrator uses PriorityGoalStrategy by default."""
        arb = GoalArbitrator()
        assert isinstance(arb.strategy, PriorityGoalStrategy)

    def test_priority_strategy_empty(self):
        """Test PriorityGoalStrategy returns None for empty list."""
        strategy = PriorityGoalStrategy()
        assert strategy.select([], WorldState()) is None

    def test_remove_goal_missing(self):
        """Test removing a non-existent goal is a no-op."""
        arb = GoalArbitrator()
        g = Goal(target_state={"a": 1}, priority=1, name="Keep")
        arb.add_goal(g)
        arb.remove_goal("Ghost")
        assert len(arb.goals) == 1
