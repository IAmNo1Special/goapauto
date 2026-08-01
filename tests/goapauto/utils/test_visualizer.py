from goapauto.models.actions import Action
from goapauto.models.goal import Goal
from goapauto.models.node import Node
from goapauto.models.worldstate import WorldState
from goapauto.utils.visualizer import SearchTreeVisualizer


class TestVisualizer:
    def _build_tree(self):
        """Build a small search tree (root + one child)."""
        viz = SearchTreeVisualizer()
        state = WorldState(val=0)
        goal = Goal(target_state={"val": 1})
        action = Action(name="test_action", preconditions={}, effects={})

        root = Node(state=state, parent=None, goal=goal)
        viz.on_node_expanded(node=root)

        child = Node(state=state, parent=root, goal=goal, action=action)
        viz.on_node_expanded(node=child)
        return viz, root, child

    def test_mermaid_generation(self):
        """Test basic mermaid output structure."""
        viz, root, child = self._build_tree()

        output = viz.to_mermaid()

        assert "graph TD" in output
        assert "test_action" in output
        assert f"{id(root)}" in output
        assert f"{id(child)}" in output

    def test_mermaid_root_label(self):
        """Test mermaid output labels the root node."""
        viz = SearchTreeVisualizer()
        state = WorldState(val=0)
        goal = Goal(target_state={"val": 1})

        root = Node(state=state, parent=None, goal=goal)
        viz.on_node_expanded(node=root)

        output = viz.to_mermaid()
        assert "Root" in output

    def test_to_graphviz(self):
        """Test Graphviz DOT output."""
        viz, root, child = self._build_tree()

        output = viz.to_graphviz()

        assert output.startswith("digraph SearchTree {")
        assert "test_action" in output
        assert "Root" in output
        assert f"{id(root)}" in output
        assert f"{id(child)}" in output
        assert output.rstrip().endswith("}")

    def test_export_mermaid_file(self, tmp_path):
        """Test saving mermaid content to a plain file."""
        viz, _, _ = self._build_tree()
        target = tmp_path / "graph.mmd"
        viz.export(str(target))

        content = target.read_text(encoding="utf-8")
        assert "graph TD" in content

    def test_export_markdown_file(self, tmp_path):
        """Test exporting to a .md file wraps in mermaid code blocks."""
        viz, _, _ = self._build_tree()
        target = tmp_path / "graph.md"
        viz.export(str(target))

        content = target.read_text(encoding="utf-8")
        assert content.startswith("```mermaid")
        assert content.rstrip().endswith("```")
        assert "graph TD" in content

    def test_clear(self):
        """Test clear resets captured data."""
        viz, _, _ = self._build_tree()
        assert len(viz.nodes) >= 1

        viz.clear()
        assert viz.nodes == {}
        assert viz.edges == []

    def test_root_node_no_parent_edge(self):
        """Test root nodes don't produce edges."""
        viz = SearchTreeVisualizer()
        state = WorldState(val=0)
        goal = Goal(target_state={"val": 1})

        root = Node(state=state, parent=None, goal=goal)
        viz.on_node_expanded(node=root)

        assert viz.nodes == {id(root): root}
        assert viz.edges == []
