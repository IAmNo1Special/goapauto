import pytest

from goapauto.models.actions import Action
from goapauto.models.goal import Goal
from goapauto.models.node import Node
from goapauto.models.worldstate import WorldState
from goapauto.utils.visualizer import SearchTreeVisualizer


class TestVisualizer:
    def test_mermaid_generation(self):
        """Test basic mermaid output structure."""
        viz = SearchTreeVisualizer()

        # Simulate node expansion
        state = WorldState(val=0)
        goal = Goal(target_state={"val": 1})
        action = Action(name="test_action", preconditions={}, effects={})

        # Root node
        root = Node(state=state, parent=None, goal=goal)
        viz.on_node_expanded(node=root)

        # Child node
        child = Node(state=state, parent=root, goal=goal, action=action)
        viz.on_node_expanded(node=child)

        output = viz.to_mermaid()

        assert "graph TD" in output
        assert "test_action" in output
        assert f"{id(root)}" in output
        assert f"{id(child)}" in output

    def test_export_file(self, tmp_path):
        """Test saving to file."""
        viz = SearchTreeVisualizer()
        viz.export(str(tmp_path / "graph.mmd"))

        assert (tmp_path / "graph.mmd").exists()
