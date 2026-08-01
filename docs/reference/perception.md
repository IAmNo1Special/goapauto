# Perception & Arbitration API Reference

## Sensors

`Sensor` and `SensorManager` provide dynamic environment perception and state updates.

```python
from goapauto.models.sensors import Sensor, SensorManager

class MySensor(Sensor):
    def sense(self) -> dict:
        return {"health": 100}

manager = SensorManager()
manager.add_sensor(MySensor())
manager.update_state(state)
```

::: goapauto.models.sensors.Sensor
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

::: goapauto.models.sensors.SensorManager
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

______________________________________________________________________

## Goal Arbitration

`GoalArbitrator` selects the highest priority satisfiable goal from a list.

```python
from goapauto.models.goal_arbitrator import GoalArbitrator

arbitrator = GoalArbitrator(goals=[goal1, goal2])
selected_goal = arbitrator.select_goal(state)
```

The `GoalArbitrator` accepts a custom `GoalSelectionStrategy` in its constructor. The default is `PriorityGoalStrategy` (lowest priority number wins).

::: goapauto.models.goal_arbitrator.GoalArbitrator
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

::: goapauto.models.goal_arbitrator.GoalSelectionStrategy
    options:
        show_root_heading: true
        show_source: false
        show_bases: false

::: goapauto.models.goal_arbitrator.PriorityGoalStrategy
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false
