# Visualizer API Reference

The `SearchTreeVisualizer` hooks into the planner to visualize the search tree.

```python
from goapauto.utils.visualizer import SearchTreeVisualizer

viz = SearchTreeVisualizer()
planner.register_hook("on_node_expanded", viz.on_node_expanded)
# ... plan ...
viz.export("tree.mmd")
```

::: goapauto.utils.visualizer.SearchTreeVisualizer
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

______________________________________________________________________

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
