import pytest

from goapauto.models.actions import Action
from goapauto.models.goal import Goal
from goapauto.models.node import Node
from goapauto.models.worldstate import WorldState


class TestNode:
    def test_init_type_error(self):
        """Test Node rejects non-WorldState."""
        with pytest.raises(TypeError, match="must be a WorldState"):
            Node(state={"a": 1}, parent=None, goal=Goal(target_state={"a": 1}))

    def test_custom_heuristic(self):
        """Test Node uses custom heuristic function."""
        state = WorldState(a=0)
        goal = Goal(target_state={"a": 5})

        node = Node(
            state=state,
            parent=None,
            goal=goal,
            heuristic_fn=lambda s, g: 42.0,
        )
        assert node.h_score == 42.0
        assert node.f_score == node.g_score + node.h_score

    def test_g_score_with_parent_and_action(self):
        """Test g_score accumulates with parent and action cost."""
        state = WorldState(a=0)
        goal = Goal(target_state={"a": 1})
        action = Action(name="inc", preconditions={}, effects={"a": 1}, cost=3.0)

        parent = Node(state=state, parent=None, goal=goal)
        child = Node(state=state, parent=parent, goal=goal, action=action)

        assert child.g_score == 3.0

    def test_g_score_action_without_cost(self):
        """Test g_score when action has no cost attribute."""
        state = WorldState(a=0)
        goal = Goal(target_state={"a": 1})

        class BareAction:
            cost = None

        parent = Node(state=state, parent=None, goal=goal)
        child = Node(state=state, parent=parent, goal=goal, action=BareAction())
        assert child.g_score == parent.g_score + 1.0

    def test_g_score_parent_without_action(self):
        """Test g_score inherits parent score when action is None."""
        state = WorldState(a=0)
        goal = Goal(target_state={"a": 1})

        parent = Node(state=state, parent=None, goal=goal)
        # Manually set a g-score then construct a child with no action
        parent.g_score = 5.0
        child = Node(state=state, parent=parent, goal=goal, action=None)
        assert child.g_score == 5.0

    def test_g_score_dict_and_list_costs(self):
        """Test g_score handling dict and list costs."""
        state = WorldState(a=0)
        goal = Goal(target_state={"a": 1})

        parent = Node(state=state, parent=None, goal=goal)

        dict_action = Action(
            name="d",
            preconditions={},
            effects={"a": 1},
            cost={"time": 2.0, "fuel": 3.0},
        )
        child_dict = Node(state=state, parent=parent, goal=goal, action=dict_action)
        assert child_dict.g_score == 5.0

        list_action = Action(
            name="l", preconditions={}, effects={"a": 1}, cost=[1.0, 2.0, 3.0]
        )
        child_list = Node(state=state, parent=parent, goal=goal, action=list_action)
        assert child_list.g_score == 6.0

    def test_heuristic_type_errors(self):
        """Test heuristic rejects invalid inputs."""
        with pytest.raises(TypeError, match="must be a WorldState"):
            Node.heuristic({"a": 1}, Goal(target_state={"a": 1}))

        with pytest.raises(TypeError, match="must be a Goal or dict"):
            Node.heuristic(WorldState(a=1), 42)  # type: ignore

    def test_heuristic_dict_goal(self):
        """Test heuristic with a dict goal."""
        state = WorldState(a=1, b=0)
        assert Node.heuristic(state, {"a": 1, "b": 1}) == 1.0
        assert Node.heuristic(state, {"a": 1, "b": 0}) == 0.0

    def test_numeric_heuristic_type_error(self):
        """Test numeric_heuristic rejects invalid inputs."""
        with pytest.raises(TypeError, match="must be a WorldState"):
            Node.numeric_heuristic({"a": 1}, Goal(target_state={"a": 1}))

        with pytest.raises(TypeError, match="must be a Goal or dict"):
            Node.numeric_heuristic(WorldState(a=1), 42)  # type: ignore

    def test_numeric_heuristic_goal_object(self):
        """Test numeric_heuristic with a Goal object."""
        state = WorldState(a=3)
        goal = Goal(target_state={"a": 7})
        assert Node.numeric_heuristic(state, goal) == 4.0

    def test_numeric_heuristic_dict(self):
        """Test numeric_heuristic with a dict goal."""
        state = WorldState(a=3)
        assert Node.numeric_heuristic(state, {"a": 7}) == 4.0

    def test_numeric_heuristic_callable(self):
        """Test numeric_heuristic with callable conditions."""
        state = WorldState(a=5)
        assert Node.numeric_heuristic(state, {"a": lambda v: v > 10}) == 1.0
        assert Node.numeric_heuristic(state, {"a": lambda v: v > 1}) == 0.0

    def test_numeric_heuristic_missing_or_non_numeric(self):
        """Test numeric_heuristic penalizes missing/non-numeric values."""
        # Missing attribute
        assert Node.numeric_heuristic(WorldState(), {"a": 7}) == 8.0
        # Non-numeric current value
        assert Node.numeric_heuristic(WorldState(a="x"), {"a": 7}) == 8.0

    def test_numeric_heuristic_non_numeric_mismatch(self):
        """Test numeric_heuristic with non-numeric target values."""
        state = WorldState(a="x")
        assert Node.numeric_heuristic(state, {"a": "y"}) == 1.0
        assert Node.numeric_heuristic(state, {"a": "x"}) == 0.0

    def test_get_path(self):
        """Test reconstructing the action path."""
        state = WorldState(a=0)
        goal = Goal(target_state={"a": 2})
        inc = Action(name="inc", preconditions={}, effects={"a": 1}, cost=1.0)
        dec = Action(name="dec", preconditions={}, effects={"a": -1}, cost=1.0)

        root = Node(state=state, parent=None, goal=goal)
        n1 = Node(state=state, parent=root, goal=goal, action=inc)
        n2 = Node(state=state, parent=n1, goal=goal, action=dec)

        path = n2.get_path()
        assert [a.name for a in path] == ["inc", "dec"]

        # Root node has empty path
        assert root.get_path() == []

    def test_get_path_with_states(self):
        """Test path reconstruction with states."""
        state = WorldState(a=0)
        goal = Goal(target_state={"a": 1})
        action = Action(name="inc", preconditions={}, effects={"a": 1}, cost=1.0)

        root = Node(state=state, parent=None, goal=goal)
        child = Node(state=state, parent=root, goal=goal, action=action)

        path = child.get_path_with_states()
        assert len(path) == 1
        assert path[0][0] is action
        assert path[0][1] == state

        # Root returns empty (skips initial None action)
        assert root.get_path_with_states() == []

    def test_depth(self):
        """Test node depth calculation."""
        state = WorldState(a=0)
        goal = Goal(target_state={"a": 2})
        action = Action(name="inc", preconditions={}, effects={"a": 1}, cost=1.0)

        root = Node(state=state, parent=None, goal=goal)
        assert root.depth() == 0

        n1 = Node(state=state, parent=root, goal=goal, action=action)
        assert n1.depth() == 1

        n2 = Node(state=state, parent=n1, goal=goal, action=action)
        assert n2.depth() == 2

    def test_ordering_comparison(self):
        """Test node ordering by f-score."""
        state = WorldState(a=0)
        goal = Goal(target_state={"a": 1})

        low = Node(state=state, parent=None, goal=goal, heuristic_fn=lambda s, g: 1.0)
        high = Node(state=state, parent=None, goal=goal, heuristic_fn=lambda s, g: 5.0)

        assert low < high
        assert not high < low
        assert low.__lt__("not-a-node") is NotImplemented

    def test_equality_and_hash(self):
        """Test node equality and hashing."""
        state = WorldState(a=0)
        goal = Goal(target_state={"a": 1})

        n1 = Node(state=state, parent=None, goal=goal)
        n2 = Node(state=state, parent=None, goal=goal)
        assert n1 == n2
        assert hash(n1) == hash(n2)

        # Different state or goal means inequality
        n3 = Node(state=WorldState(a=99), parent=None, goal=goal)
        assert n1 != n3

        assert n1.__eq__("not-a-node") is NotImplemented
        assert hash(n1) == hash(
            (hash(state), hash(frozenset(goal.target_state.items())), 0)
        )

    def test_hash_with_dict_goal(self):
        """Test hashing a node with a dict goal."""
        state = WorldState(a=0)
        node = Node(state=state, parent=None, goal={"a": 1})
        assert isinstance(hash(node), int)

    def test_str_and_repr(self):
        """Test Node string representations."""
        state = WorldState(a=1)
        goal = Goal(target_state={"a": 2})
        action = Action(name="inc", preconditions={}, effects={"a": 1}, cost=1.0)

        root = Node(state=state, parent=None, goal=goal)
        assert "action=None" in str(root)

        child = Node(state=state, parent=root, goal=goal, action=action)
        assert "inc" in str(child)
        assert "depth=1" in repr(child)
        assert "action=Action" in repr(child)
