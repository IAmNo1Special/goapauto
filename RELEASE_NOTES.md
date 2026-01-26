# Release 0.2.0: The Foundations Update

**goapauto 0.2.0** brings professional-grade tooling and strict architecture to the GOAP planner, establishing a solid foundation for complex AI agents.

## 🌟 Highlights

### 🧠 Goal Arbitration
The new **Arbitration System** allows agents to dynamically select goals based on priority and environment state. This completes the "Sense-Think-Plan-Act" loop.
- **`GoalArbitrator`**: Manages competing goals.
- **`SensorManager`**: Aggregates environmental data.

### 👁️ Visualizer
Debugging plans just got easier. Use the **Search Tree Visualizer** to export your planner's decision tree to Mermaid or Graphviz.
```python
viz = SearchTreeVisualizer()
planner.register_hook("on_node_expanded", viz.on_node_expanded)
viz.export("planning_process.mmd")
```

### 🛡️ Strict Typing (Pydantic)
We've migrated all core models (`WorldState`, `Goal`, `Action`) to **Pydantic V2**.
- **Run-time validation**: Catch configuration errors instantly.
- **Dict-like access**: `WorldState` behaves like a dictionary but validates like a model.
- **Immutable transitions**: `Action.apply()` is now safer and side-effect free.

### 📚 New Documentation
A complete rewrite of the documentation:
- **User Guide**: From zero to hero.
- **API Reference**: Detailed signatures for every class.
- **Architecture Docs**: Understanding the system design.

## 🐞 Fixes
- Standardized `PlanResult` return type (NamedTuple).
- Fixed AttributeErrors in example scripts.
- Enforced non-empty target states for Goals.

---
*Install the update:*
```bash
uv add goapauto==0.2.0
```
