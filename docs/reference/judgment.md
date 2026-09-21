# Provider-Independent Judgment

The judgment layer answers fuzzy questions about the world — *is the agent
in danger?*, *which goal next?* — through any backend implementing the
`Judge` protocol. Core (`goapauto`, no third-party imports) defines the
question/answer types, the protocol, the error contract, and two generic
layers; `JevJudge` (in `goapauto.models.jev`, behind the `jev` extra)
adapts [TypeSafe](https://typesafe.ai) System One to it.

## Questions and answers

Three frozen question types, deliberately Jev-shaped. Every question
carries an optional `metadata` side-channel that generic layers never
interpret — backends use it for round-trip identity (e.g.
`metadata["jev.question"]` holds the original SDK question, reused
verbatim by `JevJudge` so the model prompt is byte-identical).

```python
from goapauto import ChoiceQuestion, NoulQuestion, ScoreQuestion

questions = {
    "danger": NoulQuestion("Is the agent in immediate danger?"),
    "threat": ChoiceQuestion(
        "What is the nearest creature doing?",
        choices=("hunting", "sleeping"),
    ),
    "hunger": ScoreQuestion("How urgent is food?", min_value=0.0, max_value=2.0),
}
```

Answers come back as `FloatAnswer(value, confidence)` for `Noul`/`Score`
questions and `ChoiceAnswer(value, confidence)` for `Choice` questions.
Confidence is the backend's reported value, or `None` when it reports
none — never synthesized, never clamped. Out-of-range values raise
`JudgmentError(retryable=False)` instead of being clamped.

## The `Judge` protocol

```python
from goapauto import JudgmentError, JudgmentResponse

class MyJudge:
    backend = "mine"

    def judge(self, state, questions) -> JudgmentResponse: ...
    def close(self) -> None: ...
```

`judge` is synchronous: a hung backend blocks the sense→plan pipeline for
its full transport timeout. The only exception generic layers catch is
`JudgmentError(message, backend=..., retryable=...)` — `retryable` has no
default, every raise site decides. Anything else propagates unchanged
(loud by design). Telemetry names the cause chain's class, so a chained
`TypeSafeError` still reports as `"TypeSafeError"`.

## `JudgmentSensor`

A `Sensor` over any `Judge`, with the same cache/staleness/telemetry
contract as `JevSensor`: re-judges on a changed observation (when
`resense_on_change`) or an elapsed `min_interval`; absorbs retryable
failures into the stale-cache path (`max_stale=0` disables reuse);
re-raises non-retryable failures.

```python
from goapauto import JudgmentSensor
from goapauto.models.jev import JevJudge

sensor = JudgmentSensor(
    JevJudge(),  # defaults to TypeSafeClient(timeout=30.0)
    observe=lambda: {"enemies_nearby": 3, "health": 20},
    questions=questions,
    min_interval=5.0,
)
print(sensor.sense())  # {"danger": 0.87, "threat": "hunting", "hunger": 1.5}
```

`sense()` and `judge(observation)` share one cache — don't mix them on one
instance. Not thread-safe: one instance per thread.

## `JudgmentGoalStrategy`

Asks the judge one `Choice` question over the goal names and returns the
chosen goal. On retryable failure (or an unknown label) it warns and falls
back to the first goal; on non-retryable failure it re-raises `JudgmentError`.

```python
from goapauto import Goal, GoalArbitrator, JudgmentGoalStrategy
from goapauto.models.jev import JevJudge

arbitrator = GoalArbitrator(
    goals=[Goal(target_state={"fed": True}, name="Eat")],
    strategy=JudgmentGoalStrategy(JevJudge()),
)
```

## Telemetry

Pass `telemetry=` a callback receiving a `JudgmentCallRecord` per
attempted call — source, backend, latency, token usage, error,
stale-cache hit — or poll `stats()` for aggregate counters. Token counts
are copied from the backend's response; interpretation is the callback's
job.

## Offline testing

`goapauto.testing.FakeJudge` answers from canned values or a responder —
no `typesafe_sdk` import anywhere near it:

```python
from goapauto import ChoiceAnswer, FloatAnswer
from goapauto.testing import FakeJudge

judge = FakeJudge(
    answers={"danger": FloatAnswer(0.87), "threat": ChoiceAnswer("hunting")}
)
```

::: goapauto.models.judgment.Judge
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

::: goapauto.models.judgment.JudgmentError
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

::: goapauto.models.judgment.JudgmentSensor
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false

::: goapauto.models.judgment.JudgmentGoalStrategy
    options:
        show_root_heading: true
        show_source: false
        show_bases: false
        inherited_members: false
