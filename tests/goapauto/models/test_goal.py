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
