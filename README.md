# GOAP Planner

A flexible Goal-Oriented Action Planning (GOAP) system designed for game AI, but applicable to any planning problem. This implementation provides a clean, modular architecture for defining actions, goals, and world states to create intelligent AI behaviors.

## Features

- 🎯 Goal-oriented planning with customizable actions and preconditions
- 🧠 A* search algorithm for optimal plan generation
- ⚡ Efficient state management and caching
- 📊 Built-in planning statistics and debugging
- 🧩 Modular design for easy extension
- 🎮 Example implementations included

## Installation

### From PyPI
```bash
uv add goapauto
```

### From Source
1. Clone the repository:
   ```bash
   git clone https://github.com/IAmNo1Special/goapauto.git
   cd goapauto
   ```

## Quick Start

```python
from models.goap_planner import Planner, PlanResult
from models.goal import Goal
from models.actions import Action, Actions
from models.worldstate import WorldState

# Define your initial state using WorldState for better type safety and validation
initial_state = WorldState({
    'is_open': False,
    'is_focused': False,
    'has_key': True,
    'energy': 100
})

# Create actions collection
actions = Actions()

# Add actions with proper typing and validation
try:
    actions.add_action(
        name="open_door",
        preconditions={'is_open': False, 'has_key': True},
        effects={'is_open': True},
        cost=1.0
    )
    
    actions.add_action(
        name="consume_energy",
        preconditions={'energy': (lambda x: x > 0)},
        effects={'energy': (lambda x: x - 10)},
        cost=1.0
    )
except (ValueError, TypeError) as e:
    print(f"Error creating actions: {e}")
    raise

# Create a goal with validation
try:
    goal = Goal(
        name="Open Door",
        priority=1,
        target_state={'is_open': True}
    )
except ValueError as e:
    print(f"Invalid goal: {e}")
    raise

# Create and run the planner
planner = Planner(actions)
plan, message = planner.generate_plan(initial_state, goal)

# The planner will automatically display the plan and statistics
# You can access the plan and execute it if needed
if plan is not None:
    current_state = initial_state.copy()
    for i, action_name in enumerate(plan, 1):
        action = actions.get_action(action_name)
        print(f"\nStep {i}: {action_name}")
        current_state = action.apply(current_state)
        print(f"New state: {current_state}")
```

## Project Structure

- `/models` - Core GOAP implementation
  - `goap_planner.py` - Main planner logic
  - `goal.py` - Goal definition and management
  - `actions.py` - Action definitions
  - `worldstate.py` - World state management
  - `node.py` - Search node implementation

- `/examples` - Example implementations
  - `example1.py` - Basic usage example
  - `example2.py` - Advanced scenario

## Advanced Usage

### Working with WorldState

```python
from models.worldstate import WorldState

# Create a state with validation
state = WorldState({
    'health': 100,
    'has_weapon': True,
    'enemy_visible': False
})

# Safe state updates
state = state.merge({'health': 80})  # Returns new state
state = state.copy()  # Create a deep copy

# Check conditions
if state.matches({'health': (lambda x: x > 50)}):
    print("Health is above 50%")
```

### Creating Custom Actions

Define actions using the `Action` class for better type safety and validation:

```python
from models.actions import Action, Actions

# Create a single action
open_door = Action(
    name="open_door",
    preconditions={'door_locked': False},
    effects={'door_open': True},
    cost=1.0
)

# Or use the Actions collection to manage multiple actions
actions = Actions()
actions.add_action(
    name="unlock_door",
    preconditions={'has_key': True},
    effects={'door_locked': False},
    cost=1.5
)

# Add multiple actions at once
actions.add_actions([
    ("pick_up_item", {"near_item": True}, {"has_item": True}, 1.0),
    ("use_item", {"has_item": True}, {"effect_applied": True}, 1.0)
])

# Check if an action is applicable in a given state
if open_door.is_applicable(current_state):
    new_state = open_door.apply(current_state)
```

### Defininig Goals with target states and priorities

```python
from models.goal import Goal

goal = Goal(
    name="Achieve Objective",
    priority=1,
    target_state={
        'objective_complete': True
    }
)
```

### Defining Goals with Conditions

```python
from models.goal import Goal

# Simple goal
goal = Goal(
    name="Defeat Enemy",
    priority=1,
    target_state={
        'enemy_defeated': True,
        'health': (lambda x: x > 0)  # Must have health remaining
    }
)

# Goal with custom validation
try:
    goal.validate()
except ValueError as e:
    print(f"Invalid goal: {e}")
```

### Working with Plan Results

```python
# After generating a plan
if plan is not None:
    print(f"Plan found with {len(plan)} actions")
    print(f"Total cost: {planner.stats.total_cost}")
    print(f"Nodes expanded: {planner.stats.nodes_expanded}")
    print(f"Planning time: {planner.stats.execution_time:.4f}s")
    
    # Access individual plan steps
    for i, action_name in enumerate(plan, 1):
        action = actions.get_action(action_name)
        print(f"{i}. {action_name} (cost: {action.cost})")
    
    # Execute the plan and track state changes
    current_state = initial_state.copy()
    for i, action_name in enumerate(plan, 1):
        action = actions.get_action(action_name)
        print(f"\nStep {i}: {action_name}")
        current_state = action.apply(current_state)
        print(f"State after action: {current_state}")
```

### Custom Heuristics

```python
from models.goap_planner import Planner

def custom_heuristic(state, goal_state):
    """Custom heuristic function for A* search."""
    # Implement your heuristic logic here
    # Lower values mean closer to goal
    return 0  # Default to Dijkstra's algorithm

planner = Planner(actions, heuristic=custom_heuristic)
```

## Performance Tips

1. **State Design**:
   - Keep state keys simple and consistent
   - Use immutable state objects for better caching

2. **Action Design**:
   - Make preconditions as specific as possible
   - Use lambda functions for dynamic conditions
   - Keep effects minimal and focused

3. **Debugging**:
   - Enable debug logging for detailed planning info
   - Check PlanResult.stats for performance metrics
   - Use state validation to catch issues early

## Troubleshooting

### Common Issues

1. **No Plan Found**
   - Check if actions' preconditions are met
   - Verify the goal is achievable
   - Increase max_iterations if needed

2. **Performance Problems**
   - Check for infinite state spaces
   - Add more specific preconditions
   - Consider using a custom heuristic


## Running Examples

```bash
# Run basic example
python examples/example1.py

# Run advanced example
python examples/example2.py
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
