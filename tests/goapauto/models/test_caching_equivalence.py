"""Differential fuzz harness: default-mode 0.5.0 behavioral equivalence.

This is the binding backward-compatibility proof for the caching migration
(design 02, §3.2 and §6.1a). Two frozen reference models -- transcribed
line-by-line from the pre-change sources (``JevSensor._judge`` /
``_should_resense`` at 0.5.0+05, and ``JudgmentSensor._judge_observation`` /
``_absorb`` / ``_stale_values`` / ``_should_resense`` as 1:1-ported by 05) --
run lockstep against the migrated implementations under identical seeded
scripts: changing/unchanging observations, client success / retryable /
non-retryable failures, and scripted monotonic time (including the
``min_interval > max_stale`` skip-path edge and the first-call-failure
``stale`` quirk at small clock values).

Asserted identical per scenario: the ``sense()``/``judge()`` outcome
sequence (values or raised exception class), ``stats()`` counters, the full
telemetry record stream (every 0.5.0 field), and the backend call log.

The new ``JevCallRecord.staleness`` field (added by design 02 per the
coordinator's merge note) is not a 0.5.0 field: the six original fields are
compared exactly, and ``staleness`` is asserted separately against its
per-path contract (success -> "fresh", failure-path replay -> "fresh" in
default mode, failure without replay -> "dead").
"""

from __future__ import annotations

import random
import time
from collections import deque
from typing import Any

import pytest
from typesafe_sdk import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    SystemOneResponse,
    TypeSafeError,
    Usage,
)

from goapauto.models.jev import JevSensor
from goapauto.models.judgment import (
    FloatAnswer,
    JudgmentError,
    JudgmentResponse,
    JudgmentSensor,
    NoulQuestion,
    TokenUsage,
)


class _FrozenJev050:
    """Vendored transcription of the 0.5.0 JevSensor judge path.

    Transcribed from ``src/goapauto/models/jev.py`` at the pre-02 commit
    (0.5.0 + 05 merge): ``_judge`` / ``_should_resense`` / ``_record_telemetry``
    with ``system_one`` + ``_extract`` fused into a ``backend`` callable that
    returns the mapped answer dict or raises. Whole-cache state, exactly as
    0.5.0 kept it.
    """

    def __init__(
        self,
        backend,
        min_interval: float = 0.0,
        resense_on_change: bool = True,
        max_stale: float = 30.0,
    ) -> None:
        if max_stale < 0:
            raise ValueError("max_stale must be non-negative.")
        self._backend = backend
        self._min_interval = min_interval
        self._resense_on_change = resense_on_change
        self._max_stale = max_stale
        self._calls = 0
        self._errors = 0
        self._stale_cache_hits = 0
        self._total_latency_ms = 0.0
        self._cached: dict[str, Any] = {}
        self._last_observation: dict[str, Any] | None = None
        self._last_call = 0.0
        self._last_success = 0.0
        self.records: list[tuple] = []

    def sense(self, observation: dict[str, Any]) -> dict[str, Any]:
        return self._judge(observation)

    def judge(self, observation: dict[str, Any]) -> dict[str, Any]:
        return self._judge(observation)

    def stats(self) -> tuple:
        return (
            self._calls,
            self._errors,
            self._stale_cache_hits,
            self._total_latency_ms,
        )

    def _judge(self, observation: dict[str, Any]) -> dict[str, Any]:
        if self._should_resense(observation):
            start = time.monotonic()
            try:
                updates = self._backend(observation)
                latency_ms = (time.monotonic() - start) * 1000.0
                self._cached = dict(updates)
                self._last_observation = observation
                self._last_call = time.monotonic()
                self._last_success = self._last_call
                self._record("sensor", latency_ms, None, False)
            except TypeSafeError as exc:
                latency_ms = (time.monotonic() - start) * 1000.0
                stale = time.monotonic() - self._last_success <= self._max_stale
                self._record("sensor", latency_ms, type(exc).__name__, stale)
                if not stale:
                    return {}
        return dict(self._cached)

    def _should_resense(self, observation: dict[str, Any]) -> bool:
        if self._last_observation is None:
            return True
        if self._resense_on_change and observation != self._last_observation:
            return True
        return (time.monotonic() - self._last_call) >= self._min_interval

    def _record(
        self,
        source: str,
        latency_ms: float,
        error: str | None,
        stale_cache_hit: bool,
    ) -> None:
        self._calls += 1
        if error is not None:
            self._errors += 1
        if stale_cache_hit:
            self._stale_cache_hits += 1
        self._total_latency_ms += latency_ms
        self.records.append((source, latency_ms, error, stale_cache_hit))


class _FrozenJudgment050:
    """Vendored transcription of 05's JudgmentSensor judge path.

    Transcribed from ``src/goapauto/models/judgment.py`` at the pre-02
    commit: ``_should_resense`` / ``_judge_observation`` / ``_absorb`` /
    ``_stale_values`` with the ``Judge`` backend fused into a ``backend``
    callable returning the extracted value dict or raising ``JudgmentError``.
    """

    def __init__(
        self,
        backend,
        min_interval: float = 0.0,
        resense_on_change: bool = True,
        max_stale: float = 30.0,
        fail_loud: bool = True,
    ) -> None:
        if max_stale < 0:
            raise ValueError("max_stale must be non-negative.")
        self._backend = backend
        self._min_interval = min_interval
        self._resense_on_change = resense_on_change
        self._max_stale = max_stale
        self._fail_loud = fail_loud
        self._cached: dict[str, Any] | None = None
        self._last_observation: dict[str, Any] | None = None
        self._last_call = 0.0
        self._last_success = 0.0
        self._calls = 0
        self._errors = 0
        self._stale_cache_hits = 0
        self.records: list[tuple] = []

    def sense(self, observation: dict[str, Any]) -> dict[str, Any]:
        if self._should_resense(observation):
            self._last_call = time.monotonic()
            return self._judge_observation(observation)
        return self._cached if self._cached is not None else {}

    def judge(self, observation: dict[str, Any]) -> dict[str, Any]:
        self._last_call = time.monotonic()
        return self._judge_observation(observation)

    def stats(self) -> tuple:
        return (self._calls, self._errors, self._stale_cache_hits)

    def _should_resense(self, observation: dict[str, Any]) -> bool:
        if self._last_observation is None:
            return True
        if self._resense_on_change and observation != self._last_observation:
            return True
        return (time.monotonic() - self._last_call) >= self._min_interval

    def _judge_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        self._calls += 1
        start = time.monotonic()
        try:
            values = self._backend(observation)
        except JudgmentError as exc:
            latency_ms = (time.monotonic() - start) * 1000.0
            self._errors += 1
            if not exc.retryable and self._fail_loud:
                self._record(latency_ms, exc, stale_cache_hit=False)
                raise
            stale = self._absorb(exc)
            self._record(latency_ms, exc, stale_cache_hit=stale is not None)
            return stale if stale is not None else {}
        latency_ms = (time.monotonic() - start) * 1000.0
        self._last_observation = dict(observation)
        self._last_success = time.monotonic()
        self._cached = dict(values)
        self._record(latency_ms, None, stale_cache_hit=False)
        return dict(self._cached)

    def _absorb(self, exc: JudgmentError) -> dict[str, Any] | None:
        if exc.retryable:
            stale = self._stale_values()
            if stale is not None:
                self._stale_cache_hits += 1
                return stale
            return None
        stale = self._stale_values()
        if stale is not None:
            self._stale_cache_hits += 1
        return stale

    def _stale_values(self) -> dict[str, Any] | None:
        if self._cached is None or self._max_stale <= 0:
            return None
        if time.monotonic() - self._last_success <= self._max_stale:
            return self._cached
        return None

    def _record(
        self,
        latency_ms: float,
        error: JudgmentError | None,
        stale_cache_hit: bool,
    ) -> None:
        self.records.append(
            (
                "sensor",
                "scripted",
                latency_ms,
                1 if error is None else None,
                1 if error is None else None,
                type(error).__name__ if error is not None else None,
                stale_cache_hit,
            )
        )


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    return now


def _jev_questions():
    return {
        "danger": Noul(instructions="Is the agent in danger?"),
        "threat": Choice(
            instructions="What is the nearest creature doing?",
            criteria={"hunting": None, "sleeping": None},
        ),
        "hunger": Score(
            instructions="How urgent is food?",
            criteria=["not hungry", "hungry", "starving"],
        ),
    }


def _sdk_response(values: dict[str, Any]) -> SystemOneResponse:
    return SystemOneResponse(
        model="fuzz",
        usage=Usage(input_tokens=10, output_tokens=2),
        answers={
            "danger": NoulAnswer(noul=values["danger"]),
            "threat": ChoiceAnswer(
                choice=values["threat"],
                confidence=1.0,
                probabilities={values["threat"]: 1.0},
            ),
            "hunger": ScoreAnswer(
                score=values["hunger"],
                confidence=1.0,
                legend={0: "x"},
                probabilities={0: 1.0},
            ),
        },
    )


class _ScriptedJevClient:
    """JevClient whose per-call behavior is driven by a shared script."""

    def __init__(self, script: deque) -> None:
        self._script = script
        self.calls: list[dict[str, Any]] = []

    def system_one(self, state: Any, questions: Any) -> SystemOneResponse:
        self.calls.append(dict(state))
        behavior, values = self._script.popleft()
        if behavior == "ok":
            return _sdk_response(values)
        if behavior == "typesafe_error":
            raise TypeSafeError("boom")
        raise RuntimeError("boom")

    def close(self) -> None:
        pass


class _ScriptedJudge:
    backend = "scripted"

    def __init__(self, script: deque) -> None:
        self._script = script
        self.calls: list[dict[str, Any]] = []

    def judge(self, state: Any, questions: Any) -> JudgmentResponse:
        self.calls.append(dict(state))
        behavior, values = self._script.popleft()
        if behavior == "ok":
            return JudgmentResponse(
                answers={name: FloatAnswer(values[name]) for name in questions},
                backend=self.backend,
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
        raise JudgmentError(
            "boom", backend=self.backend, retryable=behavior == "retryable"
        )

    def close(self) -> None:
        pass


_OBSERVATIONS = [
    {},
    {"x": 1},
    {"x": 1, "y": [1, 2]},
    {"x": 2},
    {"s": "abc"},
    {"nested": {"a": [1, {"b": 2}]}},
]
_TIME_STEPS = [0.0, 0.1, 0.5, 2.0, 7.0, 35.0, 100.0]
_JEV_BEHAVIORS = ["ok"] * 6 + ["typesafe_error"] * 3 + ["runtime_error"]
_JUDGE_BEHAVIORS = ["ok"] * 6 + ["retryable"] * 2 + ["non_retryable"] * 2

_JEV_CONFIGS = [
    {},
    {"min_interval": 5.0},
    {"min_interval": 60.0, "max_stale": 30.0},
    {"max_stale": 0.0},
    {"resense_on_change": False, "min_interval": 10.0},
]
_JUDGE_CONFIGS = [
    {},
    {"fail_loud": False},
    {"min_interval": 60.0, "max_stale": 30.0},
    {"max_stale": 0.0},
]

_SCENARIOS = 24
_STEPS = 15


def _fuzz_script(seed: int, behaviors: list[str], start: float):
    rng = random.Random(seed)
    script = []
    now = start
    for _ in range(_STEPS):
        now += rng.choice(_TIME_STEPS)
        observation = dict(rng.choice(_OBSERVATIONS))
        behavior = rng.choice(behaviors)
        values = {
            "danger": rng.random(),
            "threat": rng.choice(["hunting", "sleeping"]),
            "hunger": rng.random() * 2.0,
            "a": rng.random(),
            "b": rng.random(),
        }
        use_judge = rng.random() < 0.15
        script.append((now, observation, behavior, values, use_judge))
    return script


def test_jev_default_mode_matches_frozen_050(clock):
    """JevSensor with defaults is observationally identical to 0.5.0."""
    for config_index, config in enumerate(_JEV_CONFIGS):
        for start_index, start in enumerate((1000.0, 5.0)):
            for seed in range(_SCENARIOS):
                script = _fuzz_script(
                    config_index * 100000 + start_index * 10000 + seed,
                    _JEV_BEHAVIORS,
                    start,
                )
                new = _run_jev_new(config, script, clock)
                ref = _run_against_frozen_jev(config, script, clock)
                assert new["outcomes"] == ref["outcomes"], (config, seed, start)
                assert _stats_tuple(new["stats"]) == ref["stats"], (
                    config,
                    seed,
                    start,
                )
                assert _jev_records_four(new["records"]) == ref["records"], (
                    config,
                    seed,
                    start,
                )
                assert new["backend_calls"] == ref["backend_calls"], (
                    config,
                    seed,
                    start,
                )
                for record in new["records"]:
                    _assert_jev_staleness_contract(record)


def _run_jev_new(config, script, clock):
    script_queue: deque = deque(
        (behavior, values) for _, _, behavior, values, _ in script
    )
    backend = _ScriptedJevClient(script_queue)
    records: list = []
    cell: list[dict[str, Any]] = [{}]
    sensor = JevSensor(
        observe=lambda: dict(cell[0]),
        questions=_jev_questions(),
        client=backend,
        telemetry=records.append,
        **config,
    )
    outcomes: list[tuple] = []
    for now, observation, _, _, use_judge in script:
        clock[0] = now
        cell[0] = observation
        try:
            if use_judge:
                outcomes.append(("ok", sensor.judge(dict(observation))))
            else:
                outcomes.append(("ok", sensor.sense()))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(("raise", type(exc).__name__))
    return {
        "outcomes": outcomes,
        "stats": sensor.stats(),
        "records": records,
        "backend_calls": list(backend.calls),
    }


def _run_against_frozen_jev(config, script, clock):
    script_queue: deque = deque(
        (behavior, values) for _, _, behavior, values, _ in script
    )
    frozen_calls: list[dict[str, Any]] = []

    def backend(observation):
        frozen_calls.append(dict(observation))
        behavior, values = script_queue.popleft()
        if behavior == "ok":
            return {
                "danger": values["danger"],
                "threat": values["threat"],
                "hunger": values["hunger"],
            }
        if behavior == "typesafe_error":
            raise TypeSafeError("boom")
        raise RuntimeError("boom")

    sensor = _FrozenJev050(backend, **config)
    outcomes: list[tuple] = []
    for now, observation, _, _, use_judge in script:
        clock[0] = now
        call = sensor.judge if use_judge else sensor.sense
        try:
            outcomes.append(("ok", call(dict(observation))))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(("raise", type(exc).__name__))
    return {
        "outcomes": outcomes,
        "stats": sensor.stats(),
        "records": sensor.records,
        "backend_calls": frozen_calls,
    }


def _stats_tuple(stats) -> tuple:
    return (
        stats.calls,
        stats.errors,
        stats.stale_cache_hits,
        stats.total_latency_ms,
    )


def _jev_records_four(records) -> list[tuple]:
    # The frozen 0.5.0 transcription does not model backend usage/tokens
    # (always None there); tokens are orthogonal to the 02 changes, so the
    # differential compares only the fields the frozen model captures.
    return [
        (
            r.source,
            r.latency_ms,
            r.error,
            r.stale_cache_hit,
        )
        for r in records
    ]


def _assert_jev_staleness_contract(record) -> None:
    """The additive staleness field follows its per-path contract."""
    assert record.staleness in ("fresh", "stale", "dead")
    if record.error is None:
        assert record.staleness == "fresh"
    elif record.stale_cache_hit:
        assert record.staleness == "fresh"  # default mode has no STALE band
    else:
        assert record.staleness == "dead"


def test_judgment_default_mode_matches_frozen_05(clock):
    """JudgmentSensor with defaults is observationally identical to 05."""
    for config_index, config in enumerate(_JUDGE_CONFIGS):
        for start_index, start in enumerate((1000.0, 5.0)):
            for seed in range(_SCENARIOS):
                script = _fuzz_script(
                    config_index * 100000 + start_index * 10000 + seed,
                    _JUDGE_BEHAVIORS,
                    start,
                )
                new = _run_judgment_new(config, script, clock)
                ref = _run_judgment_frozen(config, script, clock)
                assert new["outcomes"] == ref["outcomes"], (config, seed, start)
                assert _judgment_stats_tuple(new["stats"]) == ref["stats"], (
                    config,
                    seed,
                    start,
                )
                assert _judgment_records(new["records"]) == ref["records"], (
                    config,
                    seed,
                    start,
                )
                assert new["backend_calls"] == ref["backend_calls"], (
                    config,
                    seed,
                    start,
                )


def _run_judgment_new(config, script, clock):
    script_queue: deque = deque(
        (behavior, values) for _, _, behavior, values, _ in script
    )
    backend = _ScriptedJudge(script_queue)
    records: list = []
    cell: list[dict[str, Any]] = [{}]
    sensor = JudgmentSensor(
        judge=backend,
        observe=lambda: dict(cell[0]),
        questions={
            "a": NoulQuestion(instructions="a?"),
            "b": NoulQuestion(instructions="b?"),
        },
        telemetry=records.append,
        **config,
    )
    outcomes: list[tuple] = []
    for now, observation, _, _, use_judge in script:
        clock[0] = now
        cell[0] = observation
        try:
            if use_judge:
                outcomes.append(("ok", sensor.judge(dict(observation))))
            else:
                outcomes.append(("ok", sensor.sense()))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(("raise", type(exc).__name__))
    return {
        "outcomes": outcomes,
        "stats": sensor.stats(),
        "records": records,
        "backend_calls": list(backend.calls),
    }


def _run_judgment_frozen(config, script, clock):
    script_queue: deque = deque(
        (behavior, values) for _, _, behavior, values, _ in script
    )
    frozen_calls: list[dict[str, Any]] = []

    def backend(observation):
        frozen_calls.append(dict(observation))
        behavior, values = script_queue.popleft()
        if behavior == "ok":
            return {"a": values["a"], "b": values["b"]}
        raise JudgmentError(
            "boom", backend="scripted", retryable=behavior == "retryable"
        )

    sensor = _FrozenJudgment050(backend, **config)
    outcomes: list[tuple] = []
    for now, observation, _, _, use_judge in script:
        clock[0] = now
        call = sensor.judge if use_judge else sensor.sense
        try:
            outcomes.append(("ok", call(dict(observation))))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(("raise", type(exc).__name__))
    return {
        "outcomes": outcomes,
        "stats": sensor.stats(),
        "records": sensor.records,
        "backend_calls": frozen_calls,
    }


def _judgment_stats_tuple(stats) -> tuple:
    return (stats.calls, stats.errors, stats.stale_cache_hits)


def _judgment_records(records) -> list[tuple]:
    return [
        (
            r.source,
            r.backend,
            r.latency_ms,
            r.input_tokens,
            r.output_tokens,
            r.error,
            r.stale_cache_hit,
        )
        for r in records
    ]
