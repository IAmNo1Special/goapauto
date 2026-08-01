# User Guide: Getting Started

This guide explains how to use `goapauto` to build intelligent agents.

## 1. Define the World State

The `WorldState` is the agent's memory or belief about the world. It is initialized with keyword arguments.

```python
from goapauto.models.worldstate import WorldState

# Define initial state
state = WorldState(
    hungry=True,
    has_food=False,
    location="bedroom"
)
```

### Distinguishing Unknown from False

`WorldState` distinguishes "never set" from "explicitly False/None":

```python
from goapauto.models.worldstate import _UNKNOWN

state = WorldState()
print(state.get("x"))           # _UNKNOWN (never set)
print(state.is_known("x"))      # False

state.x = False
print(state.get("x"))           # False
print(state.is_known("x"))      # True

del state.x
print(state.get("x"))           # _UNKNOWN (deleted)
```

## 2. Define Actions

Actions transform the state. They have **preconditions** (requirements) and **effects** (changes).

```python
from goapauto.models.actions import Action, Set, Increment, Range

# Action: specific definition
eat_action = Action(
    name="eat_food",
    preconditions={"has_food": True},
    effects={
        "hungry": False, 
        "has_food": False
    },
    cost=1.0
)

# Action: using reusable Effects
move_to_kitchen = Action(
    name="goto_kitchen",
    preconditions={"location": "bedroom"},
    effects={"location": Set("kitchen")},
    cost=2.0
)

# Temporal planning with duration
build_action = Action(
    name="build_shelter",
    preconditions={"has_wood": True},
    effects={"has_shelter": True},
    cost=5.0,
    duration=30.0  # seconds
)

# Multi-dimensional costs
navigate = Action(
    name="navigate",
    preconditions={},
    effects={"location": Set("target")},
    cost={"time": 30.0, "energy": 5.0, "risk": 2.0},
    duration=30.0
)

# Range preconditions
rest_action = Action(
    name="rest",
    preconditions={"energy": Range(0, 30)},  # only if energy < 30
    effects={"energy": 100},
    cost=1.0,
    duration=10.0
)
```

## 3. Define Goals

A `Goal` describes a target state the agent wants to reach.

```python
from goapauto.models.goal import Goal
from goapauto.models.actions import GreaterThan, LessThan, Range

# Exact value goal
goal = Goal(
    target_state={"hungry": False},
    priority=1
)

# Inequality goals
survival_goal = Goal(
    target_state={
        "health": GreaterThan(50),        # health > 50
        "ammo": Range(10, 100),           # 10 <= ammo <= 100
        "thirst": LessThan(50)            # thirst < 50
    },
    priority=1
)

# Callable targets (custom logic)
custom_goal = Goal(
    target_state={
        "enemy_distance": lambda v: v > 10,  # distance > 10
        "has_weapon": lambda v: v is True
    }
)
```

## 4. Run the Planner

Feed the actions into the `Planner` and request a plan to the goal.

```python
from goapauto.models.goap_planner import Planner

# Basic planner
planner = Planner(actions_list=[
    eat_action,
    move_to_kitchen,
    build_action
])

result = planner.generate_plan(state, goal)

if result.plan:
    print("Plan found:", result.plan)
    # Output: ['goto_kitchen', 'eat_food']
else:
    print("No plan found:", result.message)
```

### Planner Configuration

```python
# Verbose output (default True)
planner = Planner(actions_list=actions, verbose=False)

# Custom logger
import logging
logger = logging.getLogger("my_planner")
planner = Planner(actions_list=actions, logger=my_logger)

# Multi-dimensional cost optimization
planner = Planner(
    actions_list=actions,
    cost_weights={"time": 1.0, "energy": 0.5, "risk": 2.0}
)

# Custom heuristic for numeric goals
from goapauto.models.node import Node
planner = Planner(actions_list=actions, heuristic_fn=Node.numeric_heuristic)

# Limit search depth
result = planner.generate_plan(state, goal, max_depth=10)
```

### PlanResult

```python
result = planner.generate_plan(state, goal)

# Access plan
print(result.plan)          # ['action1', 'action2']
print(result.message)       # success/error message
print(result.total_cost)    # total cost (float)

# Temporal schedule (if actions have duration)
if result.schedule:
    print("Makespan:", result.schedule.makespan)
    for step in result.schedule.steps:
        print(f"  {step.action}: {step.start_time}-{step.end_time}")

# Search graph for debugging/visualization
graph = planner.get_search_graph()
print(f"Nodes: {len(graph['nodes'])}, Edges: {len(graph['edges'])}")
```

## 5. Advanced Features

### Incremental Replanning

```python
# Execute part of plan
state = WorldState(x=0)
result = planner.generate_plan(state, {"x": 5})
# ['inc', 'inc', 'inc', 'inc', 'inc']

# Execute first action
state2 = apply_action(state, result.plan[0])  # x=1

# Continue from current state
remaining = planner.continue_plan(state2, {"x": 5}, executed_actions=["inc"])
print(remaining.plan)  # ['inc', 'inc', 'inc', 'inc']
```

### Goal Context for Dynamic Actions

```python
class AdaptiveProvider:
    def provide_actions(self, state, goal=None):
        if goal and "surgery" in str(goal.target_state):
            return [emergency_surgery_action]
        return [treat_patient_action]

planner = Planner(providers=[AdaptiveProvider()])
result = planner.generate_plan(state, Goal(target_state={"surgery": True}))
# Will include emergency_surgery_action
```

## 6. Execution (Advanced)

In a real game loop, you would execute the first action in the plan, then re-evaluate the state or re-plan.

See `examples/example1.py` for a complete runnable bot.
