# Visualizer API Reference

## `goapauto.utils.visualizer.SearchTreeVisualizer`

Hooks into the planner to visualize the search tree.

```python
viz = SearchTreeVisualizer()
planner.register_hook("on_node_expanded", viz.on_node_expanded)
# ... plan ...
viz.export("tree.mmd")
```

### Methods

- **`export(filepath: str) -> None`**
  Saves the tree to a file. Supports `.mmd` (Mermaid).

- **`to_mermaid() -> str`**
  Returns the raw Mermaid diagram string.

- **`to_graphviz() -> str`**
  Returns the raw Graphviz DOT string.

- **`clear()`**
  Resets the captured data.

## Search Graph Export

The planner also provides direct access to the search graph data:

```python
result = planner.generate_plan(state, goal)
graph = planner.get_search_graph()

# graph structure:
{
    "nodes": {
        node_id: {
            "id": int,
            "state": dict,
            "g": float,      # cost from start
            "h": float,      # heuristic
            "f": float,      # total
            "parent": int,
            "action": str
        }
    },
    "edges": [
        {"from": int, "to": int, "action": str, "cost": float}
    ],
    "metadata": {
        "expanded_count": int,
        "visited_count": int,
        "max_depth_reached": int
    }
}
```
