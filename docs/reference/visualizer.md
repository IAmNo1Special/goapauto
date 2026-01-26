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
