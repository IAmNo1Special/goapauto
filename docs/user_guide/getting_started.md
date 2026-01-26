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

## 2. Define Actions
Actions transform the state. They have **preconditions** (requirements) and **effects** (changes).

```python
from goapauto.models.actions import Action, Set

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
```

## 3. Define Goals
A `Goal` describes a target state the agent wants to reach.

```python
from goapauto.models.goal import Goal

goal = Goal(
    target_state={"hungry": False},
    priority=1
)
```

## 4. Run the Planner
Feed the actions into the `Planner` and request a plan to the goal.

```python
from goapauto.models.goap_planner import Planner

planner = Planner(actions_list=[
    # You can pass raw tuples or Action objects
    (eat_action.name, eat_action.preconditions, eat_action.effects, eat_action.cost),
    (move_to_kitchen.name, move_to_kitchen.preconditions, move_to_kitchen.effects, move_to_kitchen.cost)
])

result = planner.generate_plan(state, goal)

if result.plan:
    print("Plan found:", result.plan)
    # Output: ['goto_kitchen', 'eat_food']
else:
    print("No plan found:", result.message)
```

## 5. Execution (Advanced)
In a real game loop, you would Execute the first action in the plan, then re-evaluate the state or re-plan.

See `examples/example1.py` for a complete runnable bot.
