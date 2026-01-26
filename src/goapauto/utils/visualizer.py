from __future__ import annotations

import logging
from typing import Dict, List

from goapauto.models.node import Node

logger = logging.getLogger(__name__)


class SearchTreeVisualizer:
    """Utility for capturing and exporting the A* search tree.

    This visualizer hooks into the Planner's 'on_node_expanded' event
    to build a representation of the search space that can be exported
    to various formats.
    """

    def __init__(self) -> None:
        self.nodes: Dict[int, Node] = {}
        self.edges: List[tuple] = []

    def on_node_expanded(self, node: Node) -> None:
        """Capture a node and its connection to its parent.

        Args:
            node: The node being expanded
        """
        node_id = id(node)
        self.nodes[node_id] = node

        if node.parent:
            parent_id = id(node.parent)
            # Store (parent_id, child_id, action_name)
            action_name = node.action.name if node.action else "Start"
            self.edges.append((parent_id, node_id, action_name))

    def to_mermaid(self) -> str:
        """Generate a Mermaid diagram string for the search tree.

        Returns:
            A string formatted as a Mermaid flowchart (graph TD)
        """
        lines = ["graph TD"]

        # Define nodes with labels
        for node_id, node in self.nodes.items():
            # Create a label with f-score and maybe some state info
            f_score = f"{node.f_score:.1f}"
            label = f'"{node.action.name if node.action else "Root"}\\nf={f_score}"'
            lines.append(f"    {node_id}({label})")

        # Define edges with action names
        for parent_id, child_id, action_name in self.edges:
            lines.append(f"    {parent_id} -->|{action_name}| {child_id}")

        return "\n".join(lines)

    def to_graphviz(self) -> str:
        """Generate a Graphviz DOT string for the search tree.

        Returns:
            A string in DOT format
        """
        lines = ["digraph SearchTree {", "    node [shape=box, style=rounded];"]

        for node_id, node in self.nodes.items():
            f_score = f"{node.f_score:.1f}"
            label = f'{node.action.name if node.action else "Root"}\nf={f_score}'
            lines.append(f'    {node_id} [label="{label}"];')

        for parent_id, child_id, action_name in self.edges:
            lines.append(f'    {parent_id} -> {child_id} [label="{action_name}"];')

        lines.append("}")
        return "\n".join(lines)

    def export(self, filepath: str) -> None:
        """Export the search tree to a file.

        Args:
            filepath: Path to save the file. If extension is .mmd, saves mermaid.
                      Defaulting to mermaid for now.
        """
        content = self.to_mermaid()
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    def clear(self) -> None:
        """Reset the captured data."""
        self.nodes.clear()
        self.edges.clear()
