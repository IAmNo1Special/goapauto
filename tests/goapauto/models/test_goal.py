import pytest
from pydantic import ValidationError

from goapauto.models.goal import Goal
from goapauto.models.worldstate import WorldState


class TestGoal:
    def test_initialization_happy_path(self):
        """Test valid goal creation."""
        goal = Goal(target_state={"a": 1}, priority=5, name="MyGoal")
        assert goal.target_state == {"a": 1}
        assert goal.priority == 5
        assert goal.name == "MyGoal"

    def test_default_values(self):
        """Test default priority and name generation."""
        goal = Goal(target_state={"b": 2})
        assert goal.priority == 1
        assert goal.name == "{'b': 2}"

    def test_validation_errors(self):
        """Test Pydantic validation failures."""
        with pytest.raises(ValidationError):
            Goal(target_state={}, priority=1)  # Empty target state

        with pytest.raises(ValidationError):
            Goal(target_state={"a": 1}, priority=0)  # Invalid priority

    def test_satisfaction_check(self):
        """Test is_satisfied logic."""
        goal = Goal(target_state={"a": 1, "b": 2})

        # Satisfied
        assert goal.is_satisfied(WorldState(a=1, b=2, c=3))

        # Unsatisfied (missing key)
        assert not goal.is_satisfied(WorldState(a=1))

        # Unsatisfied (wrong value)
        assert not goal.is_satisfied(WorldState(a=1, b=99))

    def test_unsatisfied_conditions(self):
        """Test get_unsatisfied_conditions details."""
        goal = Goal(target_state={"a": 1, "b": 2})
        ws = WorldState(a=1, b=99)

        diff = goal.get_unsatisfied_conditions(ws)
        assert "b" in diff
        assert diff["b"] == (99, 2)
        assert "a" not in diff

    def test_equality_and_hashing(self):
        goal1 = Goal(target_state={"a": 1}, priority=1)
        goal2 = Goal(target_state={"a": 1}, priority=1)

        assert goal1 == goal2
        assert hash(goal1) == hash(goal2)

    def test_empty_target_state_validation(self):
        """Test empty target_state is rejected."""
        with pytest.raises(ValidationError):
            Goal(target_state={})

    def test_name_auto_generation(self):
        """Test name defaults to the target state string."""
        goal = Goal(target_state={"hunger": 0})
        assert goal.name == str({"hunger": 0})

    def test_satisfaction_with_callable(self):
        """Test is_satisfied with callable conditions."""
        goal = Goal(target_state={"health": lambda h: h > 50})
        assert goal.is_satisfied(WorldState(health=80))
        assert not goal.is_satisfied(WorldState(health=20))

        # Non-callable value comparison
        goal2 = Goal(target_state={"flag": True})
        assert goal2.is_satisfied(WorldState(flag=True))
        assert not goal2.is_satisfied(WorldState(flag=False))

    def test_satisfaction_attribute_error(self):
        """Test is_satisfied returns False when state has no attribute."""
        goal = Goal(target_state={"a": 1})
        # WorldState always allows extra attrs, but a missing one returns None
        assert not goal.is_satisfied(WorldState())

    def test_satisfaction_callable_attribute_error(self):
        """Test is_satisfied catches AttributeError from callable conditions."""

        def exploding(value):
            raise AttributeError("boom")

        goal = Goal(target_state={"x": exploding})
        assert not goal.is_satisfied(WorldState(x=1))

    def test_unsatisfied_conditions_callable(self):
        """Test get_unsatisfied_conditions with callable conditions."""
        goal = Goal(target_state={"health": lambda h: h > 50})
        ws = WorldState(health=20)
        diff = goal.get_unsatisfied_conditions(ws)
        assert "health" in diff
        assert diff["health"] == (20, goal.target_state["health"])

        satisfied_ws = WorldState(health=80)
        assert goal.get_unsatisfied_conditions(satisfied_ws) == {}

    def test_unsatisfied_conditions_exception(self):
        """Test get_unsatisfied_conditions propagates exceptions."""

        def exploding(value):
            raise RuntimeError("boom")

        goal = Goal(target_state={"x": exploding})
        with pytest.raises(RuntimeError, match="boom"):
            goal.get_unsatisfied_conditions(WorldState(x=1))

    def test_str_and_repr(self):
        """Test Goal string representations."""
        goal = Goal(target_state={"a": 1}, priority=2, name="MyGoal")
        assert str(goal) == "Goal(MyGoal, priority=2)"
        assert "MyGoal" in repr(goal)
        assert "priority=2" in repr(goal)
