"""Provider-independent judgment interface.

This module is the stable core of the judgment layer: questions, answers,
and the ``Judge`` protocol that every backend (Jev, a fake, a rule engine,
anything) implements. ``JudgmentSensor`` and ``JudgmentGoalStrategy`` are the
generic, backend-agnostic policy layers -- they validate answers, keep
telemetry, and absorb or propagate failures. They never import a provider.

Not thread-safe: ``JudgmentSensor`` mutates its cache without locking, same
as every other sensor in this package.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from goapauto.models.caching import (
    CachePolicy,
    SensorHealth,
    SensorReport,
    StalePolicy,
    _contains_nan,
    _PerKeyCache,
)
from goapauto.models.goal import Goal
from goapauto.models.sensors import Sensor
from goapauto.models.worldstate import WorldState

logger = logging.getLogger(__name__)

__all__ = [
    "NoulQuestion",
    "ChoiceQuestion",
    "ScoreQuestion",
    "Question",
    "FloatAnswer",
    "ChoiceAnswer",
    "Answer",
    "TokenUsage",
    "JudgmentResponse",
    "Judge",
    "JudgmentError",
    "JudgmentCallRecord",
    "JudgmentStats",
    "JudgmentSensor",
    "JudgmentGoalStrategy",
]


@dataclass(frozen=True)
class NoulQuestion:
    """A yes/no question answered with a float in [0, 1].

    ``metadata`` is a backend-namespaced side-channel (e.g. ``"jev.question"``);
    generic layers never interpret it and pass it through untouched.
    """

    instructions: str
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ChoiceQuestion:
    """Pick one label from ``choices``.

    ``metadata`` is a backend-namespaced side-channel (e.g. ``"jev.question"``);
    generic layers never interpret it and pass it through untouched.
    """

    instructions: str
    choices: tuple[str, ...]
    descriptions: Mapping[str, str] | None = None
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "choices", tuple(self.choices))
        if not self.choices or any(not label for label in self.choices):
            raise ValueError("ChoiceQuestion needs non-empty choice labels.")
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("ChoiceQuestion needs unique choice labels.")


@dataclass(frozen=True)
class ScoreQuestion:
    """A float score inside ``[min_value, max_value]``.

    ``metadata`` is a backend-namespaced side-channel (e.g. ``"jev.question"``);
    generic layers never interpret it and pass it through untouched.
    """

    instructions: str
    min_value: float = 0.0
    max_value: float = 1.0
    rubric: str | None = None
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.min_value >= self.max_value:
            raise ValueError(
                "ScoreQuestion needs min_value < max_value, "
                f"got {self.min_value} >= {self.max_value}."
            )


Question = NoulQuestion | ChoiceQuestion | ScoreQuestion
"""Any question a judge can be asked."""


@dataclass(frozen=True)
class FloatAnswer:
    """A float answer, with optional confidence in [0, 1]."""

    value: float
    confidence: float | None = None


@dataclass(frozen=True)
class ChoiceAnswer:
    """A choice answer, with optional confidence in [0, 1]."""

    value: str
    confidence: float | None = None


Answer = FloatAnswer | ChoiceAnswer
"""Any answer a judge can return."""


@dataclass(frozen=True)
class TokenUsage:
    """Token counts for one judgment call, when the backend reports them."""

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class JudgmentResponse:
    """One backend's answers, keyed by question name."""

    answers: Mapping[str, Answer]
    backend: str
    model: str | None = None
    latency_ms: float | None = None
    usage: TokenUsage | None = None


@runtime_checkable
class Judge(Protocol):
    """Anything that can answer judgment questions.

    ``judge`` is synchronous: a hung backend blocks the sense→plan pipeline
    for its full transport timeout. There is no in-flight hook in Phase 1;
    ``latency_ms`` on the telemetry record is post-call attribution only.
    The protocol guarantees nothing about caching — a bare ``judge`` call
    always goes to the backend; ``JudgmentSensor`` adds the caching layer.
    """

    backend: str

    def judge(
        self, state: Mapping[str, Any], questions: Mapping[str, Question]
    ) -> JudgmentResponse:
        """Answer ``questions`` about ``state``."""
        ...

    def close(self) -> None:
        """Release any resources held by this judge."""
        ...


class JudgmentError(Exception):
    """A judgment call failed. ``retryable`` has no default: callers must say."""

    def __init__(self, message: str, *, backend: str, retryable: bool) -> None:
        super().__init__(message)
        self.backend = backend
        self.retryable = retryable


@dataclass(frozen=True)
class JudgmentCallRecord:
    """Telemetry for one attempted judgment call."""

    source: str
    backend: str
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    stale_cache_hit: bool = False


@dataclass(frozen=True)
class JudgmentStats:
    """Aggregate counters for a sensor or strategy."""

    calls: int = 0
    errors: int = 0
    stale_cache_hits: int = 0


def _json_safe(value: Any) -> Any:
    """Convert target-state values into JSON-compatible content."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if callable(value):
        return getattr(value, "__name__", repr(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(val) for val in value]
    return str(value)


class JudgmentSensor(Sensor):
    """Sense the world through any ``Judge`` backend.

    Cache behavior mirrors the frozen 0.5.0 JevSensor exactly: the first call
    judges; a changed observation (when ``resense_on_change``) or an elapsed
    ``min_interval`` re-judges; ``min_interval=0`` re-judges every call; the
    cache updates atomically after success; ``sense()`` and ``judge()`` share
    one cache. Not thread-safe.

    ``stale_after`` opts into the per-key staleness ladder (design 02 §3.1)
    instead of the legacy whole-cache cliff: keys age through FRESH/STALE/DEAD
    and ``stale_policy`` decides whether STALE keys are kept flagged or
    dropped. ``None`` (the default) keeps the 0.5.0 behavior exactly.
    ``sense_detailed()``, ``health()``, ``diagnostics()`` and
    ``last_report()`` expose the per-key provenance; ``name`` labels the
    sensor in reports; ``observation_fingerprint`` is an optional
    change-detector used instead of raw observation comparison.
    """

    def __init__(
        self,
        judge: Judge,
        observe: Callable[[], Mapping[str, Any]],
        questions: Mapping[str, Question],
        mapping: Mapping[str, str] | None = None,
        min_interval: float = 0.0,
        resense_on_change: bool = True,
        max_stale: float = 30.0,
        telemetry: Callable[[JudgmentCallRecord], None] | None = None,
        stale_after: float | None = None,
        stale_policy: StalePolicy = StalePolicy.KEEP_FLAGGED,
        name: str | None = None,
        observation_fingerprint: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        # Assigned before the question loop below reuses ``name`` as a loop
        # variable; the parameter must not be shadowed.
        self.name = name
        if judge is None:
            raise TypeError(
                "JudgmentSensor needs a judge; core has no default backend."
            )
        if not questions:
            raise ValueError("JudgmentSensor needs at least one question.")
        for name, question in questions.items():
            if not isinstance(question, (NoulQuestion, ChoiceQuestion, ScoreQuestion)):
                raise TypeError(
                    f"Question {name!r} has unknown type "
                    f"{type(question).__name__!r}; expected NoulQuestion, "
                    "ChoiceQuestion, or ScoreQuestion."
                )
        resolved = dict(mapping) if mapping else {name: name for name in questions}
        unknown = sorted(set(resolved) - set(questions))
        if unknown:
            raise ValueError(f"Mapping refers to unknown questions: {unknown}.")
        # Validated last, as before: a bad max_stale raises only after the
        # judge/questions/mapping checks above.
        policy = CachePolicy(
            stale_after=stale_after,
            max_stale=max_stale,
            stale_policy=stale_policy,
        )
        self._judge = judge
        self._observe = observe
        self._questions = dict(questions)
        self._mapping = resolved
        self._max_stale = max_stale
        self._policy = policy
        self._min_interval = min_interval
        self._resense_on_change = resense_on_change
        self._telemetry = telemetry
        # Shared per-key cache engine (design 02 §3.3): successful reads
        # refresh the keys they report; absent keys keep value and age.
        self._cache = _PerKeyCache(policy)
        self._last_observation: Mapping[str, Any] | None = None
        self._last_call = 0.0
        self._last_report: SensorReport | None = None
        self._last_error: str | None = None
        self._observation_fingerprint = observation_fingerprint
        self._fingerprint: Any = None
        self._has_fingerprint = False
        self._stats = JudgmentStats()

    def sense(self) -> dict[str, float | str]:
        """Observe, then judge only when the cache policy requires it.

        Shares cache state with ``judge()`` — do not mix them on one instance.
        """
        return self._sense_report().values

    def sense_detailed(self) -> SensorReport:
        """Like :meth:`sense`, with per-key age and staleness metadata."""
        return self._sense_report()

    def last_report(self) -> SensorReport | None:
        """Most recent report, or None before the first report-producing call."""
        return self._last_report

    def health(self) -> SensorHealth:
        """Current band/health view over the cache; never raises."""
        return self._cache.health(
            self.name or type(self).__name__, self._last_error, time.monotonic()
        )

    def diagnostics(self) -> dict[str, Any]:
        """Exact design §6 payload; ``{}`` before the first report."""
        return self._cache.diagnostics(self._last_report, time.monotonic())

    def _sense_report(self) -> SensorReport:
        observation = self._observe()
        if self._should_resense(observation):
            self._last_call = time.monotonic()
            return self._judge_report(observation)
        return self._skip_report()

    def judge(self, observation: Mapping[str, Any]) -> dict[str, float | str]:
        """Judge an explicit observation, bypassing the resense check.

        Shares cache state with ``sense()`` — do not mix them on one instance.
        """
        self._last_call = time.monotonic()
        return self._judge_report(observation).values

    def backend_name(self) -> str:
        return getattr(self._judge, "backend", "unknown") or "unknown"

    def stats(self) -> JudgmentStats:
        return JudgmentStats(
            calls=self._stats.calls,
            errors=self._stats.errors,
            stale_cache_hits=self._stats.stale_cache_hits,
        )

    def close(self) -> None:
        self._judge.close()

    def _should_resense(self, observation: Mapping[str, Any]) -> bool:
        if self._last_observation is None:
            return True
        if self._resense_on_change:
            if self._observation_fingerprint is not None:
                # Change detection via fingerprint. The previous fingerprint
                # is computed on demand from the last observation: it is
                # never consulted on the very first sense, so a raising
                # fingerprint only breaks the call that actually compares.
                current_fp = self._observation_fingerprint(observation)
                previous_fp = (
                    self._fingerprint
                    if self._has_fingerprint
                    else self._observation_fingerprint(self._last_observation)
                )
                self._fingerprint = current_fp
                self._has_fingerprint = True
                changed = current_fp != previous_fp
                if changed:
                    return True
            if self._observation_fingerprint is None:
                changed = (
                    _contains_nan(observation)
                    or _contains_nan(self._last_observation)
                    or observation != self._last_observation
                )
                if changed:
                    return True
        return (time.monotonic() - self._last_call) >= self._min_interval

    def _judge_report(self, observation: Mapping[str, Any]) -> SensorReport:
        name = self.name or type(self).__name__
        self._stats = JudgmentStats(
            calls=self._stats.calls + 1,
            errors=self._stats.errors,
            stale_cache_hits=self._stats.stale_cache_hits,
        )
        start = time.monotonic()
        try:
            response = self._judge.judge(dict(observation), dict(self._questions))
            values = self._extract(response, dict(self._questions))
        except JudgmentError as exc:
            latency_ms = (time.monotonic() - start) * 1000.0
            self._stats = JudgmentStats(
                calls=self._stats.calls,
                errors=self._stats.errors + 1,
                stale_cache_hits=self._stats.stale_cache_hits,
            )
            self._last_error = type(exc).__name__
            if not exc.retryable:
                self._record_telemetry(latency_ms, exc, stale_cache_hit=False)
                raise
            report = self._absorb_report(exc, name)
            self._record_telemetry(latency_ms, exc, stale_cache_hit=report is not None)
            if report is None:
                report = SensorReport(values={}, meta={}, sensor=name)
            self._last_report = report
            return report
        latency_ms = (time.monotonic() - start) * 1000.0
        now = time.monotonic()
        self._cache.write(values, now)
        self._last_observation = dict(observation)
        self._last_error = None
        self._record_telemetry(
            latency_ms, None, stale_cache_hit=False, usage=response.usage
        )
        values_out, meta = self._cache.serve(now, live_keys=values.keys(), source=name)
        report = SensorReport(values=values_out, meta=meta, sensor=name)
        self._last_report = report
        return report

    def _skip_report(self) -> SensorReport:
        """Skip-path serve: nothing due, replay the cache without an age check."""
        name = self.name or type(self).__name__
        values, meta = self._cache.serve(
            time.monotonic(), live_keys=(), source=name, skip_age_check=True
        )
        report = SensorReport(values=values, meta=meta, sensor=name)
        self._last_report = report
        return report

    def _absorb_report(self, exc: JudgmentError, name: str) -> SensorReport | None:
        """Degrade a retryable JudgmentError to a cached report or None.

        Non-retryable errors raise before this is reached.
        """
        now = time.monotonic()
        report = self._serve_failure(now, name)
        if report is not None:
            logger.warning(
                "Judgment failed with retryable error; reusing stale cache: %s",
                exc,
            )
            self._stats = JudgmentStats(
                calls=self._stats.calls,
                errors=self._stats.errors,
                stale_cache_hits=self._stats.stale_cache_hits + 1,
            )
            return report
        logger.error("Judgment failed with retryable error; no stale cache: %s", exc)
        return None

    def _serve_failure(self, now: float, name: str) -> SensorReport | None:
        """Failure-path serve: cached view within policy, else None."""
        if self._policy.stale_after is None:
            # 05's `_stale_values`, via the shared per-key store: the
            # whole-cache age gate against the last successful instant.
            if self._policy.max_stale <= 0:
                return None
            last_success = self._cache.last_success_instant()
            if last_success is None:
                return None
            if now - last_success > self._policy.max_stale:
                return None
            values, meta = self._cache.serve(
                now, live_keys=(), source=name, skip_age_check=True
            )
            return SensorReport(values=values, meta=meta, sensor=name)
        values, meta = self._cache.serve(now, live_keys=(), source=name)
        if not values:
            return None
        return SensorReport(values=values, meta=meta, sensor=name)

    def _extract(
        self, response: JudgmentResponse, questions: Mapping[str, Question]
    ) -> dict[str, float | str]:
        missing = sorted(set(questions) - set(response.answers))
        if missing:
            raise JudgmentError(
                f"Missing answers for questions: {missing}.",
                backend=response.backend,
                retryable=False,
            )
        extra = sorted(set(response.answers) - set(questions))
        if extra:
            logger.warning("Ignoring unexpected answers for questions: %s.", extra)
        values: dict[str, float | str] = {}
        for name, state_key in self._mapping.items():
            answer = response.answers[name]
            values[state_key] = self._validate(
                name, self._questions[name], answer, response.backend
            )
        return values

    def _validate(
        self, name: str, question: Question, answer: Answer, backend: str
    ) -> float | str:
        if isinstance(question, ChoiceQuestion):
            if not isinstance(answer, ChoiceAnswer):
                raise JudgmentError(
                    f"Wrong answer type for question {name!r}: expected "
                    f"ChoiceAnswer, got {type(answer).__name__}.",
                    backend=backend,
                    retryable=False,
                )
            if answer.value not in question.choices:
                raise JudgmentError(
                    f"Answer {answer.value!r} for question {name!r} not in "
                    f"choices {list(question.choices)}.",
                    backend=backend,
                    retryable=False,
                )
            self._validate_confidence(name, answer.confidence, backend)
            return answer.value
        if not isinstance(answer, FloatAnswer):
            raise JudgmentError(
                f"Wrong answer type for question {name!r}: expected "
                f"FloatAnswer, got {type(answer).__name__}.",
                backend=backend,
                retryable=False,
            )
        value = answer.value
        if not isinstance(value, (int, float)):
            raise JudgmentError(
                f"Answer value {value!r} for question {name!r} is not a number.",
                backend=backend,
                retryable=False,
            )
        low, high = (
            (0.0, 1.0)
            if isinstance(question, NoulQuestion)
            else (
                question.min_value,
                question.max_value,
            )
        )
        if not low <= value <= high:
            raise JudgmentError(
                f"Answer value {value!r} for question {name!r} out of "
                f"range [{low}, {high}]; values are never clamped.",
                backend=backend,
                retryable=False,
            )
        self._validate_confidence(name, answer.confidence, backend)
        return value

    @staticmethod
    def _validate_confidence(name: str, confidence: float | None, backend: str) -> None:
        if confidence is not None and (
            not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0
        ):
            raise JudgmentError(
                f"Confidence {confidence!r} for question {name!r} out of range [0, 1].",
                backend=backend,
                retryable=False,
            )

    def _record_telemetry(
        self,
        latency_ms: float,
        error: JudgmentError | None,
        stale_cache_hit: bool,
        usage: TokenUsage | None = None,
    ) -> None:
        record = JudgmentCallRecord(
            source="sensor",
            backend=self.backend_name(),
            latency_ms=latency_ms,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            error=_error_name(error),
            stale_cache_hit=stale_cache_hit,
        )
        self._emit_telemetry(record)

    def _emit_telemetry(self, record: JudgmentCallRecord) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry(record)
        except Exception:
            logger.exception("Telemetry callback failed; continuing.")


def _error_name(error: JudgmentError | None) -> str | None:
    if error is None:
        return None
    cause = error.__cause__
    return type(cause).__name__ if cause is not None else type(error).__name__


class JudgmentGoalStrategy:
    """Pick a goal by asking any ``Judge`` backend one Choice question.

    Retryable failures warn and fall back to the first goal; non-retryable
    failures re-raise ``JudgmentError``.
    """

    def __init__(
        self,
        judge: Judge,
        instructions: str = "Which goal should the agent pursue next?",
        telemetry: Callable[[JudgmentCallRecord], None] | None = None,
    ) -> None:
        if judge is None:
            raise TypeError(
                "JudgmentGoalStrategy needs a judge; core has no default backend."
            )
        self._judge = judge
        self._instructions = instructions
        self._telemetry = telemetry
        self._stats = JudgmentStats()

    def select(self, goals: list[Goal], state: WorldState) -> Goal | None:
        if not goals:
            return None
        labels = [
            goal.name if goal.name is not None else str(goal.target_state)
            for goal in goals
        ]
        duplicates = sorted({label for label in labels if labels.count(label) > 1})
        if duplicates:
            raise ValueError(
                f"JudgmentGoalStrategy needs unique goal names: {duplicates}."
            )
        self._stats = JudgmentStats(
            calls=self._stats.calls + 1,
            errors=self._stats.errors,
            stale_cache_hits=self._stats.stale_cache_hits,
        )
        start = time.monotonic()
        try:
            question = ChoiceQuestion(
                instructions=self._instructions,
                choices=tuple(labels),
                descriptions={
                    label: json.dumps(
                        {
                            "target_state": _json_safe(goal.target_state),
                            "priority": goal.priority,
                        },
                        sort_keys=True,
                    )
                    for label, goal in zip(labels, goals, strict=True)
                },
            )
            judge_state = {
                "world_state": state.to_dict(),
                "goals": [
                    {"name": label, "target_state": _json_safe(goal.target_state)}
                    for label, goal in zip(labels, goals, strict=True)
                ],
            }
            response = self._judge.judge(judge_state, {"goal": question})
            label = self._extract_pick(response)
        except JudgmentError as exc:
            latency_ms = (time.monotonic() - start) * 1000.0
            self._stats = JudgmentStats(
                calls=self._stats.calls,
                errors=self._stats.errors + 1,
                stale_cache_hits=self._stats.stale_cache_hits,
            )
            self._record_telemetry(latency_ms, exc)
            if exc.retryable:
                logger.warning(
                    "Goal judgment failed with retryable error; "
                    "falling back to first goal: %s",
                    exc,
                )
                return goals[0]
            raise
        latency_ms = (time.monotonic() - start) * 1000.0
        self._record_telemetry(latency_ms, None, usage=response.usage)
        if label not in labels:
            logger.warning(
                "Judge selected unknown goal %r; falling back to first goal.", label
            )
            return goals[0]
        return goals[labels.index(label)]

    def backend_name(self) -> str:
        return getattr(self._judge, "backend", "unknown") or "unknown"

    def stats(self) -> JudgmentStats:
        return JudgmentStats(
            calls=self._stats.calls,
            errors=self._stats.errors,
            stale_cache_hits=self._stats.stale_cache_hits,
        )

    def close(self) -> None:
        self._judge.close()

    @staticmethod
    def _extract_pick(response: JudgmentResponse) -> str:
        try:
            answer = response.answers["goal"]
        except KeyError:
            raise JudgmentError(
                "Missing answer for question 'goal'.",
                backend=response.backend,
                retryable=False,
            ) from None
        if not isinstance(answer, ChoiceAnswer):
            raise JudgmentError(
                "Wrong answer type for goal question: expected ChoiceAnswer, "
                f"got {type(answer).__name__}.",
                backend=response.backend,
                retryable=False,
            )
        return answer.value

    def _record_telemetry(
        self,
        latency_ms: float,
        error: JudgmentError | None,
        usage: TokenUsage | None = None,
    ) -> None:
        if self._telemetry is None:
            return
        record = JudgmentCallRecord(
            source="strategy",
            backend=self.backend_name(),
            latency_ms=latency_ms,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            error=_error_name(error),
        )
        try:
            self._telemetry(record)
        except Exception:
            logger.exception("Telemetry callback failed; continuing.")
