# GOAP Planner

A flexible Goal-Oriented Action Planning (GOAP) system designed for game AI and general planning problems. This implementation provides a clean, strictly-typed, modular architecture for defining intelligent behaviors.

## Features

- 🎯 **Goal-Oriented**: customizable actions, preconditions, and goals.
- 🧠 **A* Search**: finds the optimal plan to satisfy detailed world states.
- ⚡ **Strict API**: Built on Pydantic for validation and type safety.
- 📊 **Visual & Observable**: Built-in visualization tools (Mermaid) and event hooks.
- 🧩 **Modular**: Decoupled Sensors, Goals, and Arbitrators.

## Documentation

- **[User Guide](docs/user_guide.md)**: Step-by-step tutorial for building your first agent.
- **[API Reference](docs/api_reference.md)**: Detailed documentation of classes and methods.
- **[Architecture](docs/architecture.md)**: High-level system design and data flow.
- **[Examples](examples/)**: Runnable scripts demonstrating the planner in action.

## Installation

### From PyPI
```bash
uv add goapauto
```

### From Source
```bash
git clone https://github.com/IAmNo1Special/goapauto.git
cd goapauto
uv sync
```

## Quick Start
See the [User Guide](docs/user_guide.md) for a complete walkthrough.

```python
from goapauto.models.worldstate import WorldState
from goapauto.models.goal import Goal
from goapauto.models.actions import Action, Actions
from goapauto.models.goap_planner import Planner

# 1. Define State
state = WorldState(has_key=True, is_open=False)

# 2. Define Actions
open_door = Action(
    name="open_door",
    preconditions={"has_key": True, "is_open": False},
    effects={"is_open": True},
    cost=1.0
)

# 3. Define Goal
goal = Goal(target_state={"is_open": True})

# 4. Plan
planner = Planner(actions_list=[open_door])
result = planner.generate_plan(state, goal)

print(f"Plan: {result.plan}")
# Output: Plan: ['open_door']
```

## Contributing
Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License
MIT
