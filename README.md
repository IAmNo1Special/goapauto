# GOAP Planner (`goapauto`)

> **Goal-Oriented Action Planning for Python** — Build intelligent, autonomous agents with strict typing and comprehensive tooling.

![CI](https://github.com/IAmNo1Special/goapauto/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/IAmNo1Special/goapauto)
![License](https://img.shields.io/github/license/IAmNo1Special/goapauto)

`goapauto` provides a modular framework for AI decision-making using A* search. It separates **Perception** (Sensors), **Thinking** (Arbitration), **Planning** (A*), and **Acting** (Actions), making it ideal for game AI, NPCs, and simulation bots.

## 🚀 Key Features (v0.2.0)

- 🎯 **Goal Arbitration**: Dynamically select the best goal based on priority and state.
- 🧠 **Smart Planning**: A* pathfinding finds the optimal sequence of actions.
- 🛡️ **Type Safety**: Built on [Pydantic](https://docs.pydantic.dev/) for strict validation and robustness.
- 👁️ **Visualizer**: Export search trees to [Mermaid](https://mermaid.js.org/) or Graphviz for debugging.
- 🔌 **Modular Architecture**: Decoupled components for `WorldState`, `Sensors`, and `Goals`.

## 📚 Documentation

Detailed documentation is available in the [`docs/`](docs/) directory:

- 🏁 **[Getting Started](docs/user_guide/getting_started.md)**: Build your first agent in minutes.
- 📖 **[Core Concepts](docs/user_guide/core_concepts.md)**: Understand the Sense-Think-Plan-Act loop.
- ⚙️ **[API Reference](docs/index.md#api-reference)**: Deep dive into classes (`WorldState`, `Planner`, etc.).

## 📦 Installation

```bash
uv add goapauto
# or
uv add goapauto
```

## ⚡ Quick Start

Here's a minimal example of an agent figuring out how to open a door.

```python
from goapauto.models.worldstate import WorldState
from goapauto.models.goal import Goal
from goapauto.models.actions import Action
from goapauto.models.goap_planner import Planner

# 1. The World: Agent has a key, but the door is closed.
state = WorldState(has_key=True, is_open=False)

# 2. The Action: Needs 'has_key' to open the door.
open_door = Action(
    name="open_door",
    preconditions={"has_key": True, "is_open": False},
    effects={"is_open": True},
    cost=1.0
)

# 3. The Goal: We want the door open.
goal = Goal(target_state={"is_open": True})

# 4. The Plan: Find the path.
planner = Planner(actions_list=[open_door])
result = planner.generate_plan(state, goal)

print(f"Plan to '{goal.name}': {result.plan}")
# Output: Plan to "{'is_open': True}": ['open_door']
```

## 🛠️ Advanced Tooling

### Visualization
Debug your planner's decision-making process by exporting the search tree:

```python
from goapauto.utils.visualizer import SearchTreeVisualizer

viz = SearchTreeVisualizer()
planner.register_hook("on_node_expanded", viz.on_node_expanded)

# ... run plan ...

# Save as a Mermaid diagram
viz.export("planning_tree.mmd")
```

## 🤝 Contributing
Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) and our [Code of Conduct](CODE_OF_CONDUCT.md).

## 📄 License
MIT License. See [LICENSE](LICENSE).
