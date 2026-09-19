# TypeSafe Judgments

`JevSensor` and `JevGoalStrategy` plug [TypeSafe](https://typesafe.ai) System
One judgments into the two places where GOAP meets fuzzy reality: perception
and goal selection.

A `JevSensor` sends a raw observation as TypeSafe *state* and maps the typed
answers onto world-state keys (`noul` → probability, `choice` → label,
`score` → rubric value). The planner keeps reasoning over crisp values; only
the perception step uses the model. Judgments are cached and re-queried when
the observation changes or `min_interval` elapses.

```python
from typesafe_sdk import Noul

from goapauto import JevSensor, SensorManager, WorldState

sensor = JevSensor(
    observe=lambda: {"enemies_nearby": 3, "health": 20},
    questions={
        "danger": Noul(instructions="Is the agent in immediate danger?"),
    },
    min_interval=5.0,
)
state = WorldState()
SensorManager(sensors=[sensor]).update_state(state)
print(state.danger)  # e.g. 0.87
```

A `JevGoalStrategy` implements the `GoalSelectionStrategy` protocol: it asks
Jev which active goal to pursue, choosing between the goals by name. Both
take the official [`typesafe-sdk`](https://docs.typesafe.ai/sdk/python)
`TypeSafeClient` directly — no wrapper.

```python
from typesafe_sdk import TypeSafeClient

from goapauto import Goal, GoalArbitrator, JevGoalStrategy

client = TypeSafeClient()
arbitrator = GoalArbitrator(
    goals=[Goal(target_state={"fed": True}, name="Eat")],
    strategy=JevGoalStrategy(client=client),
)
```

Both need a TypeSafe API key (`TYPESAFE_API_KEY`). On failure they degrade
gracefully: the sensor reuses its last judgments, the strategy falls back to
the first goal.

::: goapauto.models.jev.JevSensor
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

::: goapauto.models.jev.JevGoalStrategy
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false
