# Perception & Arbitration API Reference

## `goapauto.models.sensors.SensorManager`

Orchestrates multiple `Sensor` instances to update `WorldState`.

```python
manager = SensorManager()
manager.add_sensor(MySensor())
manager.update_state(state)
```

### Methods
- **`add_sensor(sensor: Sensor)`**: Register a sensor.
- **`update_state(state: WorldState) -> WorldState`**: Runs all sensors and updates the state.

## `goapauto.models.sensors.Sensor` (Abstract)
Base class for sensors. Implement `sense() -> Dict[str, Any]`.

---

## `goapauto.models.goal_arbitrator.GoalArbitrator`

Selects the highest priority satisfiable goal from a list.

```python
arbitrator = GoalArbitrator(goals=[goal1, goal2])
selected_goal = arbitrator.select_goal(state)
```

### Methods
- **`add_goal(goal: Goal)`**: Add a goal to manage.
- **`remove_goal(name: str)`**: Remove a goal by its name.
- **`select_goal(state: WorldState) -> Optional[Goal]`**: Returns best valid goal.

### Configuration
The `GoalArbitrator` accepts a custom `GoalSelectionStrategy` in its constructor. The default is `PriorityGoalStrategy` (lowest priority number wins).
