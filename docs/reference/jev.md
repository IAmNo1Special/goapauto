# TypeSafe Judgments

`JevSensor` and `JevGoalStrategy` plug [TypeSafe](https://typesafe.ai) System
One judgments into the two places where GOAP meets fuzzy reality: perception
and goal selection. They live behind the optional `jev` extra:

```bash
uv add "goapauto[jev]"
```

Without the extra, importing `goapauto` still works — only the `Jev*`
symbols (resolved lazily) raise a helpful error telling you to install it.

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

## Injecting observations

`sense()` pulls an observation through the `observe` callback. When the host
builds observations on its own thread (a game loop holding DB access, say)
and hands the snapshot to a worker thread that owns the client, use
`judge(observation)` instead — same cache, staleness, and telemetry, but the
observation is supplied by the caller. `sense()` and `judge()` share cache
state, so don't mix them on one sensor.

## Sharing one client

By default each class constructs and owns its own `TypeSafeClient`. To share
one client (one connection pool, one timeout config), use `shared_client`:

```python
from goapauto import JevGoalStrategy, JevSensor, shared_client

with shared_client(timeout=8.0) as client:
    sensor = JevSensor(observe=..., questions=..., client=client)
    strategy = JevGoalStrategy(client=client)
```

A client passed explicitly keeps its caller-owned lifecycle; only defaulted
clients are closed by their owner.

## Telemetry

Pass `telemetry=` a callback receiving a `JevCallRecord` per attempted call
(source, latency, token usage, error, stale-cache hit), or poll `stats()`
for aggregate counters. Both are plain data — no logging framework or
vendor lock-in — so a host can calibrate thresholds from real judgments:

```python
records = []
sensor = JevSensor(observe=..., questions=..., telemetry=records.append)
...
print(sensor.stats())  # JevStats(calls=..., errors=..., ...)
```

## Offline testing

`goapauto.testing.FakeTypeSafeClient` answers with canned values — no key,
no network — for tests and `--demo` runs. See
`examples/jev_reference.py` for the canonical loop.

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
