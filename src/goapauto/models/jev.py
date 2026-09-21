from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

try:
    from typesafe_sdk import (
        Choice,
        Noul,
        Questions,
        Score,
        ScoreAnswer,
        SystemOneResponse,
        TypeSafeAPIConnectionError,
        TypeSafeAPIError,
        TypeSafeAPIResponseValidationError,
        TypeSafeAPITimeoutError,
        TypeSafeAuthenticationError,
        TypeSafeBadRequestError,
        TypeSafeClient,
        TypeSafeError,
        TypeSafeInternalServerError,
        TypeSafeNotFoundError,
        TypeSafePermissionDeniedError,
        TypeSafeRateLimitError,
        TypeSafeUnprocessableEntityError,
    )
except ImportError as exc:
    raise ImportError(
        'goapauto Jev support requires the "jev" extra: uv add "goapauto[jev]"'
    ) from exc

from goapauto.models.caching import (
    CachePolicy,
    SensorHealth,
    SensorReport,
    Staleness,
    StalePolicy,
    ValueMeta,
    _contains_nan,
    _PerKeyCache,
)
from goapauto.models.goal import Goal
from goapauto.models.goal_arbitrator import PriorityGoalStrategy
from goapauto.models.judgment import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    FloatAnswer,
    JudgmentError,
    JudgmentResponse,
    NoulQuestion,
    Question,
    ScoreQuestion,
    TokenUsage,
)
from goapauto.models.sensors import Sensor
from goapauto.models.worldstate import WorldState

logger = logging.getLogger(__name__)

__all__ = [
    "JevSensor",
    "JevGoalStrategy",
    "JevJudge",
    "JevClient",
    "JevCallRecord",
    "JevStats",
    "GateDecision",
    "shared_client",
    "TypeSafeClient",
    "TypeSafeError",
]


# Valid question objects for JevSensor: the official typesafe-sdk classes.
# (The SDK's *Model TypedDicts describe raw-dict shapes, which we reject.)
_QUESTION_TYPES = (Noul, Choice, Score)


class JevClient(Protocol):
    """Minimal client surface the Jev classes need.

    ``TypeSafeClient`` satisfies this, as does ``FakeTypeSafeClient`` from
    ``goapauto.testing``. Implement ``system_one`` and ``close`` to plug in
    a custom transport.
    """

    def system_one(
        self, state: Any, questions: Any, **kwargs: Any
    ) -> SystemOneResponse:
        """Answer named questions about state."""
        ...

    def close(self) -> None:
        """Release any resources held by the client."""
        ...


@dataclass(frozen=True)
class GateDecision:
    """One confidence-gate outcome for a single question (sensor) or pick.

    ``decision`` is ``"proceed"`` or ``"blocked"``; ``reason`` explains why
    (``"passed"`` | ``"low_confidence"`` | ``"absent_confidence"``);
    ``fallback`` names what happened instead of trusting the value
    (``"drop"`` for a dropped sensor update, ``"first"``/``"priority"`` for a
    strategy fallback, ``None`` when the value was trusted). Invariants are
    enforced at construction: blocked iff low_confidence, fallback set iff
    blocked, confidence None iff absent_confidence.
    """

    question: str
    key: str | None
    confidence: float | None
    threshold: float
    decision: Literal["proceed", "blocked"]
    reason: Literal["passed", "low_confidence", "absent_confidence"]
    fallback: Literal["drop", "first", "priority"] | None

    def __post_init__(self) -> None:
        if (self.decision == "blocked") != (self.reason == "low_confidence"):
            raise ValueError(
                "GateDecision invariant violated: decision='blocked' iff "
                f"reason='low_confidence' (got {self.decision!r}, {self.reason!r})."
            )
        if (self.fallback is None) != (self.decision == "proceed"):
            raise ValueError(
                "GateDecision invariant violated: fallback is None iff "
                f"decision='proceed' (got {self.fallback!r}, {self.decision!r})."
            )
        if (self.confidence is None) != (self.reason == "absent_confidence"):
            raise ValueError(
                "GateDecision invariant violated: confidence is None iff "
                f"reason='absent_confidence' (got {self.confidence!r}, "
                f"{self.reason!r})."
            )
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError(
                f"GateDecision threshold must be in [0.0, 1.0], got {self.threshold!r}."
            )
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                "GateDecision confidence must be in [0.0, 1.0], "
                f"got {self.confidence!r}."
            )


@dataclass(frozen=True)
class JevCallRecord:
    """One attempted TypeSafe call, for telemetry callbacks."""

    source: str  # "sensor" or "strategy"
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    error: str | None  # exception class name, None on success
    stale_cache_hit: bool
    gate_decisions: tuple[GateDecision, ...] = ()
    gated_questions: tuple[str, ...] = ()
    unknown_confidence_questions: tuple[str, ...] = ()
    gating_enabled: bool = False
    fully_gated: bool = False
    gated: bool = False  # strategy only: the pick was gated
    confidence_absent: bool = False  # strategy only: pick had no confidence
    pick_label: str | None = None  # strategy only: what the model chose
    label_matched: bool = True  # strategy only: the pick named a real goal
    fallback_reason: Literal["error", "unknown_label", "low_confidence"] | None = None
    # Design 04 §9.2 merge note: 04's gate fields (gate_decisions through
    # fallback_reason) precede this field; the coordinator resolves the final
    # ordering at merge. Appended last so existing positional constructions
    # keep working.
    staleness: str = "fresh"  # "fresh" | "stale" | "dead": band of served data


@dataclass(frozen=True)
class JevStats:
    """Aggregate telemetry counters; see ``JevSensor.stats()``."""

    calls: int = 0
    errors: int = 0
    stale_cache_hits: int = 0
    total_latency_ms: float = 0.0
    gated: int = 0


def _require_response(response: object) -> SystemOneResponse:
    """Return the SDK response, rejecting anything else.

    The SDK's ``system_one`` always returns a validated ``SystemOneResponse``;
    anything else means the client was miswired. Raw dict responses are not
    supported.
    """
    if not isinstance(response, SystemOneResponse):
        raise TypeError(
            f"Expected SystemOneResponse, got {type(response).__name__!r}; "
            "raw dict responses are not supported."
        )
    return response


class _CorruptConfidenceError(TypeSafeError):
    """Corrupt gated confidence: NaN, out of [0, 1], or not a number.

    A private subclass of the public ``TypeSafeError``. Hosts catching
    ``TypeSafeError`` see the public contract; ``_judge``/``select`` use the
    subclass to tell corrupt data apart from provider failures: corrupt
    confidence is recorded and re-raised (fail-loud), never absorbed as an
    ordinary retryable failure and never counted as gated.
    """


def _validate_threshold(value: float, name: str) -> float:
    """Fail-fast threshold check: a float in [0.0, 1.0]; NaN fails the range."""
    if not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a float in [0.0, 1.0], got {value!r}.")
    return value


def _answer_confidence(question: Noul | Choice | Score, answer: Any) -> float | None:
    """Effective confidence for one answer; None when the backend reported none.

    Noul answers never carry confidence (Ruling C): the value is the judgment,
    so there is nothing to gate on. Corrupt reported confidences (NaN, outside
    [0, 1], not a number) raise ``TypeSafeError`` -- fail-loud, never clamped
    into a gate decision. Only called for questions with an effective
    threshold, so gating-off paths never touch the confidence attribute.
    """
    if isinstance(question, Noul):
        return None
    confidence = answer.confidence
    if confidence is None:
        return None
    if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        raise _CorruptConfidenceError(
            f"Confidence {confidence!r} is not a float in [0.0, 1.0]; "
            "the judgment data is corrupt."
        )
    return confidence


@dataclass(frozen=True)
class _GateInfo:
    """Internal carrier: one call's gate outcomes, shared by sensor/strategy."""

    decisions: tuple[GateDecision, ...] = ()
    gating_enabled: bool = False
    blocked_questions: tuple[str, ...] = ()
    unknown_confidence_questions: tuple[str, ...] = ()
    fully_gated: bool = False
    gated_pick: bool = False
    confidence_absent: bool = False
    pick_label: str | None = None
    label_matched: bool = True
    fallback_reason: Literal["error", "unknown_label", "low_confidence"] | None = None


def _worst_band_value(meta: Mapping[str, ValueMeta]) -> str:
    """Worst staleness band in a served report, as a ``JevCallRecord`` value."""
    rank = {Staleness.FRESH: 0, Staleness.STALE: 1, Staleness.DEAD: 2}
    worst_rank = 0
    for entry in meta.values():
        band_rank = rank[entry.staleness]
        if band_rank > worst_rank:
            worst_rank = band_rank
    return ("fresh", "stale", "dead")[worst_rank]


class JevSensor(Sensor):
    """World-state sensor backed by TypeSafe judgments.

    Classical sensors hand-code thresholds (``danger = enemies_nearby > 3``).
    A JevSensor instead sends a raw observation as TypeSafe *state* and maps
    the typed answers onto world-state keys:

    - ``noul`` answers become floats in ``[0, 1]``
    - ``choice`` answers become the selected label
    - ``score`` answers become floats on the question's rubric

    The planner keeps reasoning over crisp values; only the perception step
    uses the model. Judgments are cached: the API is re-queried when the
    observation changes (if ``resense_on_change``) or once ``min_interval``
    seconds have passed. On failure the last good judgments are reused so
    the agent loop keeps running -- but only for ``max_stale`` seconds;
    after that the cache is considered dead and ``sense()`` returns no
    updates (degrading visibly instead of acting on ancient judgments).

    ``stale_after`` opts into the staleness ladder: keys age per-key through
    FRESH/STALE/DEAD bands (``stale_policy`` decides whether STALE keys are
    kept flagged or dropped) instead of the legacy whole-cache cliff. The
    default ``None`` keeps the 0.5.0 behavior exactly. ``sense_detailed()``,
    ``health()``, ``diagnostics()`` and ``last_report()`` expose the
    per-key provenance; ``observation_fingerprint`` is an optional
    change-detector used instead of raw observation comparison.

    Questions must be official ``typesafe-sdk`` question objects (``Noul``,
    ``Choice``, ``Score``); raw dictionaries are rejected with ``TypeError``.
    The client is the SDK's ``TypeSafeClient`` directly.

    Thread safety: not thread-safe. Use one JevSensor per thread; do not
    share it across threads without external synchronization.
    """

    def __init__(
        self,
        observe: Callable[[], dict[str, Any]],
        questions: Questions,
        mapping: dict[str, str] | None = None,
        client: JevClient | None = None,
        min_interval: float = 0.0,
        resense_on_change: bool = True,
        max_stale: float = 30.0,
        telemetry: Callable[[JevCallRecord], None] | None = None,
        confidence_threshold: float | None = None,
        confidence_thresholds: dict[str, float] | None = None,
        stale_after: float | None = None,
        stale_policy: StalePolicy = StalePolicy.KEEP_FLAGGED,
        name: str | None = None,
        observation_fingerprint: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        # Assigned before the question loops below reuse ``name`` as a loop
        # variable; the parameter must not be shadowed.
        self.name = name
        if not questions:
            raise TypeSafeError("JevSensor needs at least one question.")
        # Validated before the question-type loop, as before: a bad max_stale
        # raises even when the questions are also bad.
        policy = CachePolicy(
            stale_after=stale_after,
            max_stale=max_stale,
            stale_policy=stale_policy,
        )
        validated: dict[str, Noul | Choice | Score] = {}
        for name, question in questions.items():
            if not isinstance(question, _QUESTION_TYPES):
                raise TypeError(
                    f"Question {name!r} must be a typesafe-sdk question "
                    f"(Noul, Choice, Score), got {type(question).__name__!r}; "
                    "raw dict questions are not supported."
                )
            validated[name] = question
        self._observe = observe
        self._questions = validated
        self._mapping = dict(mapping) if mapping else {n: n for n in questions}
        unknown = set(self._mapping) - set(questions)
        if unknown:
            raise TypeSafeError(
                f"Mapping refers to unknown questions: {sorted(unknown)}"
            )
        # Confidence gating is opt-in: no threshold anywhere means the
        # gate never runs and confidence is never read or validated.
        self._confidence_threshold = (
            _validate_threshold(confidence_threshold, "confidence_threshold")
            if confidence_threshold is not None
            else None
        )
        per_question: dict[str, float] = {}
        if confidence_thresholds:
            for name, value in confidence_thresholds.items():
                if name not in self._mapping:
                    raise TypeSafeError(
                        f"Confidence threshold for unknown question {name!r}: "
                        "thresholds are keyed by question name (the keys of "
                        "`questions`), not by mapped state key."
                    )
                if value is None:
                    raise ValueError(
                        f"Confidence threshold for question {name!r} must be a "
                        "float in [0.0, 1.0], got None."
                    )
                per_question[name] = _validate_threshold(
                    value, f"confidence_thresholds[{name!r}]"
                )
        self._confidence_thresholds = per_question
        effective: dict[str, float] = {}
        for name in self._mapping:
            if name in per_question:
                effective[name] = per_question[name]
            elif self._confidence_threshold is not None:
                effective[name] = self._confidence_threshold
        self._effective_thresholds = effective
        self._gating_enabled = bool(effective)
        # The SDK's default timeout is 10s; the pre-SDK client used 30s.
        # Keep the long-standing effective default on migration.
        self._client = client if client is not None else TypeSafeClient(timeout=30.0)
        self._owns_client = client is None
        self._min_interval = min_interval
        self._resense_on_change = resense_on_change
        self._max_stale = max_stale
        self._policy = policy
        self._telemetry = telemetry
        self._calls = 0
        self._errors = 0
        self._stale_cache_hits = 0
        self._total_latency_ms = 0.0
        self._gated = 0
        self._last_gate = _GateInfo()
        # Shared per-key cache engine (design 02 §3.3): successful reads
        # refresh the keys they report; absent keys keep value and age.
        self._cache = _PerKeyCache(policy)
        self._last_observation: dict[str, Any] | None = None
        self._last_call: float = 0.0
        self._last_report: SensorReport | None = None
        self._last_error: str | None = None
        self._observation_fingerprint = observation_fingerprint
        self._fingerprint: Any = None
        self._has_fingerprint = False

    def stats(self) -> JevStats:
        """Return aggregate telemetry for this sensor's TypeSafe calls."""
        return JevStats(
            calls=self._calls,
            errors=self._errors,
            stale_cache_hits=self._stale_cache_hits,
            total_latency_ms=self._total_latency_ms,
            gated=self._gated,
        )

    def _record_telemetry(
        self,
        source: str,
        latency_ms: float,
        usage: Any | None,
        error: str | None,
        stale_cache_hit: bool,
        gate: _GateInfo,
        staleness: str = "fresh",
    ) -> None:
        self._calls += 1
        if error is not None:
            self._errors += 1
        if stale_cache_hit:
            self._stale_cache_hits += 1
        self._total_latency_ms += latency_ms
        self._gated += len(gate.blocked_questions)
        if self._telemetry is None:
            return
        record = JevCallRecord(
            source=source,
            latency_ms=latency_ms,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            error=error,
            stale_cache_hit=stale_cache_hit,
            gate_decisions=gate.decisions,
            gated_questions=gate.blocked_questions,
            unknown_confidence_questions=gate.unknown_confidence_questions,
            gating_enabled=gate.gating_enabled,
            fully_gated=gate.fully_gated,
            staleness=staleness,
        )
        try:
            self._telemetry(record)
        except Exception:
            logger.exception("Jev telemetry callback raised; ignoring")

    def close(self) -> None:
        """Close the TypeSafe client if this sensor created it.

        Releases the underlying HTTP connection pool. A client passed in
        by the caller is left open; its lifecycle belongs to the caller.
        """
        if self._owns_client:
            self._client.close()

    def sense(self) -> dict[str, Any]:
        """Return judgment-derived state updates, re-querying only when due.

        When a re-judge fails, the last good judgments are reused only while
        they are younger than ``max_stale`` seconds; older than that, the
        cache is dead and an empty dict is returned so the agent degrades
        visibly instead of acting on stale perception.
        """
        return self._judge_report(self._observe()).values

    def sense_detailed(self) -> SensorReport:
        """Like :meth:`sense`, with per-key age and staleness metadata."""
        return self._judge_report(self._observe())

    def last_report(self) -> SensorReport | None:
        """Most recent report, or None before the first report-producing call."""
        return self._last_report

    def health(self) -> SensorHealth:
        """Current band/health view over the cache; never raises."""
        return self._cache.health(
            self.name or type(self).__name__, self._last_error, time.monotonic()
        )

    def diagnostics(self) -> dict[str, Any]:
        """Queryable sensor summary for hosts; never raises.

        Merges the 02 caching diagnostics (§9.4: ``data_age_seconds``,
        ``staleness``, ``dead_keys``, ``worst_staleness``) with the 04
        confidence-gating summary (§5e). Design 06 consumes both key sets
        from this one mapping. Before the first report only the gating
        keys appear — there is no cache data yet.
        """
        merged: dict[str, Any] = {
            "gating_enabled": self._gating_enabled,
            "effective_thresholds": dict(self._effective_thresholds),
            "gated_questions_last_call": list(self._last_gate.blocked_questions),
            "fully_gated_last_call": self._last_gate.fully_gated,
            "gated_total": self._gated,
        }
        # Never raises: a corrupt _last_report drops the caching keys rather
        # than breaking the queryable summary.
        if isinstance(self._last_report, SensorReport):
            merged.update(self._cache.diagnostics(self._last_report, time.monotonic()))
        return merged

    def judge(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Judge an injected observation instead of pulling one via ``observe``.

        Same cache, staleness, and telemetry semantics as ``sense()``; the
        only difference is where the observation comes from. This lets a host
        build the observation on its own thread (e.g. holding DB access) and
        hand the snapshot to a worker thread that owns the client.

        ``sense()`` and ``judge()`` share cache state: do not mix them on
        one instance.
        """
        return self._judge(observation)

    def _judge(self, observation: dict[str, Any]) -> dict[str, Any]:
        return self._judge_report(observation).values

    def _judge_report(self, observation: dict[str, Any]) -> SensorReport:
        name = self.name or type(self).__name__
        if self._should_resense(observation):
            start = time.monotonic()
            try:
                response = self._client.system_one(observation, self._questions)
                latency_ms = (time.monotonic() - start) * 1000.0
                updates = self._extract(response)
                # Per-key merge (Ruling A1): gated questions are absent from
                # `updates`, so their cached entries keep the previous value
                # and age. With gating off `updates` covers every mapped key,
                # so this is exactly the old whole-cache assignment.
                now = time.monotonic()
                self._cache.write(updates, now)
                self._last_observation = observation
                self._last_call = now
                self._last_error = None
                self._record_telemetry(
                    "sensor",
                    latency_ms,
                    response.usage,
                    None,
                    False,
                    self._last_gate,
                    "fresh",
                )
                values, meta = self._cache.serve(
                    now, live_keys=updates.keys(), source=name, only_live_keys=True
                )
            except TypeSafeError as exc:
                latency_ms = (time.monotonic() - start) * 1000.0
                now = time.monotonic()
                self._last_error = type(exc).__name__
                values, meta, replayed = self._serve_failure(now)
                self._last_gate = _GateInfo(gating_enabled=self._gating_enabled)
                self._record_telemetry(
                    "sensor",
                    latency_ms,
                    None,
                    type(exc).__name__,
                    replayed and not isinstance(exc, _CorruptConfidenceError),
                    self._last_gate,
                    _worst_band_value(meta) if replayed else "dead",
                )
                if isinstance(exc, _CorruptConfidenceError):
                    raise
                if replayed:
                    logger.warning(
                        "JevSensor failed; reusing last judgments", exc_info=True
                    )
                else:
                    logger.error(
                        "JevSensor failed and cached judgments are older than "
                        "max_stale (%.1fs); returning no updates",
                        self._max_stale,
                        exc_info=True,
                    )
                    values, meta = {}, {}
            except Exception as exc:
                self._last_error = type(exc).__name__
                raise
        else:
            values, meta = self._serve_skip()
        report = SensorReport(values=values, meta=meta, sensor=name)
        self._last_report = report
        return report

    def _serve_failure(
        self, now: float
    ) -> tuple[dict[str, Any], dict[str, ValueMeta], bool]:
        """Failure-path serve. Returns (values, meta, replayed)."""
        name = self.name or type(self).__name__
        if self._policy.stale_after is None:
            # §3.2 rule 4: 0.5.0's whole-cache stale check, computed off the
            # shared success instant (rule 3) -- numerically identical,
            # including the 0.0 sentinel before the first success.
            last_success = self._cache.last_success_instant()
            anchor = last_success if last_success is not None else 0.0
            if now - anchor <= self._max_stale:
                values, meta = self._cache.serve(
                    now, live_keys=(), source=name, skip_age_check=True
                )
                return values, meta, True
            return {}, {}, False
        values, meta = self._cache.serve(now, live_keys=(), source=name)
        return values, meta, bool(values)

    def _serve_skip(self) -> tuple[dict[str, Any], dict[str, ValueMeta]]:
        """Skip-path serve: change-gate or interval not yet elapsed."""
        name = self.name or type(self).__name__
        now = time.monotonic()
        if self._policy.stale_after is None:
            # §3.2 rule 2: no age check on the skip path, even past max_stale.
            return self._cache.serve(
                now, live_keys=(), source=name, skip_age_check=True
            )
        return self._cache.serve(now, live_keys=(), source=name)

    def _should_resense(self, observation: dict[str, Any]) -> bool:
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

    def _extract(self, response: SystemOneResponse) -> dict[str, Any]:
        """Map the SDK's typed answers onto world-state keys.

        Reads through the SDK's own typed accessors (``nouls()``,
        ``choices()``, ``scores()``); the question objects fixed at
        construction decide which accessor each answer comes from.

        With no confidence threshold configured this is the whole story. When
        gating is enabled, below-threshold answers are dropped from the
        returned updates (their cached entries survive via the per-key merge
        in ``_judge``) and the gate outcomes are stashed on
        ``self._last_gate`` for telemetry.
        """
        updates, gate = self._extract_gated(response)
        self._last_gate = gate
        return updates

    def _extract_gated(
        self, response: SystemOneResponse
    ) -> tuple[dict[str, Any], _GateInfo]:
        """Extract updates plus per-question gate outcomes.

        The threshold lookup is hoisted: ``_answer_confidence`` is only
        called for questions that actually have an effective threshold, so
        gating-off paths never touch the confidence attribute.
        """
        response = _require_response(response)
        updates: dict[str, Any] = {}
        decisions: list[GateDecision] = []
        blocked: list[str] = []
        unknown: list[str] = []
        for question_name, state_key in self._mapping.items():
            question = self._questions[question_name]
            threshold = self._effective_thresholds.get(question_name)
            try:
                if isinstance(question, Noul):
                    answer_obj: Any = response.nouls[question_name]
                    value: Any = answer_obj.noul
                elif isinstance(question, Choice):
                    answer_obj = response.choices[question_name]
                    value = answer_obj.choice
                else:  # Score: the only remaining validated question type.
                    answer_obj = response.scores[question_name]
                    value = answer_obj.score
            except KeyError as exc:
                raise TypeSafeError(
                    f"Missing answer for question {question_name!r}"
                ) from exc
            if threshold is None:
                updates[state_key] = value
                continue
            confidence = _answer_confidence(question, answer_obj)
            if confidence is None:
                # Noul answers (or backends reporting none): Ruling C --
                # never judged, never blocked, but flagged in telemetry.
                decisions.append(
                    GateDecision(
                        question_name,
                        state_key,
                        None,
                        threshold,
                        "proceed",
                        "absent_confidence",
                        None,
                    )
                )
                unknown.append(question_name)
                updates[state_key] = value
            elif confidence < threshold:
                decisions.append(
                    GateDecision(
                        question_name,
                        state_key,
                        confidence,
                        threshold,
                        "blocked",
                        "low_confidence",
                        "drop",
                    )
                )
                blocked.append(question_name)
            else:
                decisions.append(
                    GateDecision(
                        question_name,
                        state_key,
                        confidence,
                        threshold,
                        "proceed",
                        "passed",
                        None,
                    )
                )
                updates[state_key] = value
        if decisions:
            logger.info(
                "Gating low-confidence judgments: blocked=[%s]; absent_confidence=%s",
                ", ".join(
                    f"{d.question!r} (confidence={d.confidence} "
                    f"< threshold={d.threshold})"
                    for d in decisions
                    if d.decision == "blocked"
                ),
                unknown,
            )
        gate = _GateInfo(
            decisions=tuple(decisions),
            gating_enabled=self._gating_enabled,
            blocked_questions=tuple(blocked),
            unknown_confidence_questions=tuple(unknown),
            fully_gated=self._gating_enabled and len(blocked) == len(self._mapping),
        )
        return updates, gate


def _json_safe(value: Any) -> Any:
    """Convert target-state values into SDK-compatible JSON content."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if callable(value):
        return getattr(value, "__name__", repr(value))
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


class JevGoalStrategy:
    """GoalSelectionStrategy that asks Jev which active goal to pursue.

    Builds a ``choice`` question with one labeled option per goal (the goal
    name, described by its target state and priority) and selects the goal
    Jev picks. Satisfied goals never reach the model: ``GoalArbitrator``
    filters them before calling ``select``.

    Goal names must be unique. If the model is unreachable or names an
    unknown goal, the first goal is returned so the agent loop keeps running.

    Thread safety: not thread-safe. Use one JevGoalStrategy per thread; do
    not share it across threads without external synchronization.
    """

    def __init__(
        self,
        client: JevClient | None = None,
        instructions: str = "Which goal should the agent pursue next?",
        telemetry: Callable[[JevCallRecord], None] | None = None,
        confidence_threshold: float | None = None,
        fallback_policy: Literal["first", "priority"] = "first",
    ) -> None:
        # See JevSensor: preserve the pre-SDK 30s effective default timeout.
        self._client = client if client is not None else TypeSafeClient(timeout=30.0)
        self._owns_client = client is None
        self._instructions = instructions
        self._telemetry = telemetry
        self._confidence_threshold = (
            _validate_threshold(confidence_threshold, "confidence_threshold")
            if confidence_threshold is not None
            else None
        )
        if fallback_policy not in ("first", "priority"):
            raise ValueError(
                "fallback_policy must be 'first' or 'priority', "
                f"got {fallback_policy!r}."
            )
        self._fallback_policy = fallback_policy
        self._calls = 0
        self._errors = 0
        self._total_latency_ms = 0.0
        self._gated = 0

    def stats(self) -> JevStats:
        """Return aggregate telemetry for this strategy's TypeSafe calls."""
        return JevStats(
            calls=self._calls,
            errors=self._errors,
            total_latency_ms=self._total_latency_ms,
            gated=self._gated,
        )

    def _record_telemetry(
        self,
        latency_ms: float,
        usage: Any | None,
        error: str | None,
        gate: _GateInfo,
    ) -> None:
        self._calls += 1
        if error is not None:
            self._errors += 1
        self._total_latency_ms += latency_ms
        self._gated += len(gate.blocked_questions)
        if self._telemetry is None:
            return
        record = JevCallRecord(
            source="strategy",
            latency_ms=latency_ms,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            error=error,
            stale_cache_hit=False,
            gate_decisions=gate.decisions,
            gating_enabled=gate.gating_enabled,
            gated=gate.gated_pick,
            confidence_absent=gate.confidence_absent,
            pick_label=gate.pick_label,
            label_matched=gate.label_matched,
            fallback_reason=gate.fallback_reason,
        )
        try:
            self._telemetry(record)
        except Exception:
            logger.exception("Jev telemetry callback raised; ignoring")

    def close(self) -> None:
        """Close the TypeSafe client if this strategy created it.

        Releases the underlying HTTP connection pool. A client passed in
        by the caller is left open; its lifecycle belongs to the caller.
        """
        if self._owns_client:
            self._client.close()

    def select(self, goals: list[Goal], state: WorldState) -> Goal | None:
        """Select the goal Jev judges most worth pursuing right now.

        When ``confidence_threshold`` is set, a pick reported below the
        threshold is gated: the strategy falls back via ``fallback_policy``
        instead of trusting the pick. Corrupt reported confidence (NaN, out
        of [0, 1]) is fail-loud: it is recorded and re-raised as
        ``TypeSafeError``, never treated as a low-confidence pick. With
        ``confidence_threshold=None`` (the default) this is byte-identical
        to the 0.5.0 behavior.
        """
        if not goals:
            return None
        labeled = [
            (goal.name if goal.name is not None else str(goal.target_state), goal)
            for goal in goals
        ]
        labels = [label for label, _ in labeled]
        duplicates = {label for label in labels if labels.count(label) > 1}
        if duplicates:
            raise ValueError(
                f"JevGoalStrategy needs unique goal names: {sorted(duplicates)}"
            )
        criteria = {
            label: {
                "target_state": _json_safe(goal.target_state),
                "priority": goal.priority,
            }
            for label, goal in labeled
        }
        goal_question = Choice(
            instructions=self._instructions,
            criteria=criteria,
        )
        threshold = self._confidence_threshold
        start = time.monotonic()
        response: SystemOneResponse | None = None
        try:
            response = _require_response(
                self._client.system_one(
                    {
                        "world_state": state.to_dict(),
                        "goals": [
                            {
                                "name": label,
                                "target_state": _json_safe(goal.target_state),
                            }
                            for label, goal in labeled
                        ],
                    },
                    {"goal": goal_question},
                )
            )
            try:
                pick_answer = response.choices["goal"]
            except KeyError as exc:
                raise TypeSafeError("Missing answer for question 'goal'") from exc
            pick_label = pick_answer.choice
            confidence: float | None = None
            if threshold is not None:
                confidence = _answer_confidence(goal_question, pick_answer)
        except TypeSafeError as exc:
            latency_ms = (time.monotonic() - start) * 1000.0
            self._record_telemetry(
                latency_ms,
                response.usage if response is not None else None,
                type(exc).__name__,
                _GateInfo(
                    gating_enabled=threshold is not None,
                    fallback_reason="error",
                ),
            )
            if isinstance(exc, _CorruptConfidenceError):
                raise
            logger.warning(
                "JevGoalStrategy failed; falling back to first goal", exc_info=True
            )
            return self._fallback(goals, state)
        latency_ms = (time.monotonic() - start) * 1000.0
        label_matched = any(label == pick_label for label, _ in labeled)
        if threshold is not None and confidence is not None and confidence < threshold:
            self._record_telemetry(
                latency_ms,
                response.usage,
                None,
                _GateInfo(
                    decisions=(
                        GateDecision(
                            question="goal",
                            key=None,
                            confidence=confidence,
                            threshold=threshold,
                            decision="blocked",
                            reason="low_confidence",
                            fallback=self._fallback_policy,
                        ),
                    ),
                    gating_enabled=True,
                    blocked_questions=("goal",),
                    gated_pick=True,
                    pick_label=pick_label,
                    label_matched=label_matched,
                    fallback_reason="low_confidence",
                ),
            )
            logger.warning(
                "JevGoalStrategy blocked low-confidence pick %r "
                "(confidence=%.3f < threshold=%.3f); falling back via %r policy",
                pick_label,
                confidence,
                threshold,
                self._fallback_policy,
            )
            return self._fallback(goals, state)
        if threshold is not None:
            reason: Literal["passed", "absent_confidence"] = (
                "passed" if confidence is not None else "absent_confidence"
            )
            gate = _GateInfo(
                decisions=(
                    GateDecision(
                        question="goal",
                        key=None,
                        confidence=confidence,
                        threshold=threshold,
                        decision="proceed",
                        reason=reason,
                        fallback=None,
                    ),
                ),
                gating_enabled=True,
                confidence_absent=confidence is None,
                pick_label=pick_label,
                label_matched=label_matched,
            )
        else:
            gate = _GateInfo(pick_label=pick_label, label_matched=label_matched)
        matched: Goal | None = None
        for label, goal in labeled:
            if label == pick_label:
                matched = goal
                break
        if matched is None:
            logger.warning(
                "Jev chose unknown goal %r; falling back to first goal", pick_label
            )
            gate = replace(gate, fallback_reason="unknown_label", label_matched=False)
            self._record_telemetry(latency_ms, response.usage, None, gate)
            return self._fallback(goals, state)
        self._record_telemetry(latency_ms, response.usage, None, gate)
        return matched

    def _fallback(self, goals: list[Goal], state: WorldState) -> Goal | None:
        """Static fallback when the model pick cannot be trusted.

        ``"first"`` reproduces the 0.5.0 behavior exactly; ``"priority"``
        delegates to ``PriorityGoalStrategy`` (``min`` is stable, so priority
        ties resolve to the earliest goal in list order).
        """
        if self._fallback_policy == "priority":
            return PriorityGoalStrategy().select(goals, state)
        return goals[0]


@contextmanager
def shared_client(**kwargs: Any) -> Iterator[TypeSafeClient]:
    """Yield one ``TypeSafeClient`` shared by a sensor and a strategy.

    ``JevSensor`` and ``JevGoalStrategy`` each default to owning their own
    client; sharing one halves the connection pools and keeps timeout config
    in one place. The client is closed on context exit. A client passed
    explicitly to either class keeps its caller-owned lifecycle instead.

    Example:
        >>> with shared_client(timeout=8.0) as client:
        ...     sensor = JevSensor(observe=..., questions=..., client=client)
        ...     strategy = JevGoalStrategy(client=client)
    """
    client = TypeSafeClient(**kwargs)
    try:
        yield client
    finally:
        client.close()


class JevJudge:
    """A ``Judge`` backend over a ``TypeSafeClient`` (the Jev provider).

    Translates the provider-independent questions of
    ``goapauto.models.judgment`` to SDK questions and back. A core question
    whose ``metadata`` carries the exact SDK question under ``"jev.question"``
    is forwarded verbatim; the rest are converted mechanically
    (``ScoreQuestion`` via the canonical rung ladder below).

    Client ownership mirrors ``JevSensor``: a client passed in stays
    caller-owned; the default client is owned here and closed idempotently.
    No exception named ``TypeSafeError`` ever crosses ``judge``: provider
    failures and bad answers surface as ``JudgmentError`` chained from the
    original.
    """

    backend = "jev"

    def __init__(self, client: JevClient | None = None) -> None:
        self._client = client if client is not None else TypeSafeClient(timeout=30.0)
        self._owned = client is None
        self._closed = False

    def judge(
        self, state: Mapping[str, Any], questions: Mapping[str, Question]
    ) -> JudgmentResponse:
        if not questions:
            raise JudgmentError(
                "JevJudge needs at least one question.",
                backend=self.backend,
                retryable=False,
            )
        sdk_questions = {
            name: self._translate_question(question)
            for name, question in questions.items()
        }
        try:
            sdk_response = self._client.system_one(dict(state), sdk_questions)
        except TypeSafeError as exc:
            raise JudgmentError(
                str(exc), backend=self.backend, retryable=_retryable(exc)
            ) from exc
        response = _require_response(sdk_response)
        try:
            return self._translate_response(response, questions, sdk_questions)
        except TypeSafeError as exc:
            raise JudgmentError(
                str(exc), backend=self.backend, retryable=False
            ) from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owned:
            self._client.close()

    @staticmethod
    def _translate_question(question: Question) -> Noul | Choice | Score:
        metadata = question.metadata
        if metadata is not None:
            verbatim = metadata.get("jev.question")
            if verbatim is not None:
                return verbatim
        if isinstance(question, NoulQuestion):
            return Noul(instructions=question.instructions)
        if isinstance(question, ChoiceQuestion):
            descriptions = question.descriptions
            criteria = {
                label: (
                    descriptions[label]
                    if descriptions and label in descriptions
                    else label
                )
                for label in question.choices
            }
            return Choice(instructions=question.instructions, criteria=criteria)
        return _score_question(question)

    def _translate_response(
        self,
        response: SystemOneResponse,
        questions: Mapping[str, Question],
        sdk_questions: Mapping[str, Noul | Choice | Score],
    ) -> JudgmentResponse:
        answers = {
            name: self._translate_answer(name, question, response, sdk_questions[name])
            for name, question in questions.items()
        }
        usage = response.usage
        return JudgmentResponse(
            answers=answers,
            backend=self.backend,
            model=response.model,
            usage=TokenUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            ),
        )

    @staticmethod
    def _translate_answer(
        name: str,
        question: Question,
        response: SystemOneResponse,
        sdk_question: Noul | Choice | Score,
    ) -> Answer:
        try:
            if isinstance(question, NoulQuestion):
                value = response.nouls[name].noul
                _check_range(name, value, 0.0, 1.0)
                return FloatAnswer(value, None)
            if isinstance(question, ChoiceQuestion):
                answer = response.choices[name]
                _check_confidence(name, answer.confidence)
                return ChoiceAnswer(answer.choice, answer.confidence)
            sdk_answer = response.scores[name]
            return _score_answer(name, question, sdk_question, sdk_answer)
        except KeyError as exc:
            raise TypeSafeError(f"Missing answer for question {name!r}") from exc


def _score_question(question: ScoreQuestion) -> Score:
    """Derive the canonical SDK rung ladder for a core ``ScoreQuestion``."""
    span = question.max_value - question.min_value
    rungs = max(2, round(span) + 1)
    values = [question.min_value + span * index / (rungs - 1) for index in range(rungs)]
    instructions = question.instructions
    if question.rubric:
        instructions = f"{instructions}\nRubric: {question.rubric}"
    return Score(instructions=instructions, criteria=[f"{value:g}" for value in values])


def _score_answer(
    name: str,
    question: ScoreQuestion,
    sdk_question: Noul | Choice | Score,
    sdk_answer: ScoreAnswer,
) -> FloatAnswer:
    """Map an SDK score rung index linearly into the core interval."""
    criteria = sdk_question.criteria if isinstance(sdk_question, Score) else []
    rung_count = len(criteria or [])
    index = sdk_answer.score
    if not isinstance(index, (int, float)) or not 0 <= index <= rung_count - 1:
        raise JudgmentError(
            f"Score answer for question {name!r} has invalid rung index {index!r}.",
            backend="jev",
            retryable=False,
        )
    if rung_count < 2:
        value = question.min_value
    else:
        value = question.min_value + (
            question.max_value - question.min_value
        ) * index / (rung_count - 1)
    _check_confidence(name, sdk_answer.confidence)
    return FloatAnswer(value, sdk_answer.confidence)


def _check_range(name: str, value: float, low: float, high: float) -> None:
    if not isinstance(value, (int, float)) or not low <= value <= high:
        raise JudgmentError(
            f"Answer value {value!r} for question {name!r} out of "
            f"range [{low}, {high}]; values are never clamped.",
            backend="jev",
            retryable=False,
        )


def _check_confidence(name: str, confidence: float | None) -> None:
    if confidence is not None and (
        not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0
    ):
        raise JudgmentError(
            f"Confidence {confidence!r} for question {name!r} out of range [0, 1].",
            backend="jev",
            retryable=False,
        )


def _retryable(error: TypeSafeError) -> bool:
    """Whether a Jev failure is worth retrying. Most-specific checks first."""
    if isinstance(error, TypeSafeRateLimitError):
        return True
    if isinstance(error, (TypeSafeAPITimeoutError, TypeSafeAPIConnectionError)):
        return True
    if isinstance(error, TypeSafeInternalServerError):
        return True
    if isinstance(error, TypeSafeAPIResponseValidationError):
        return False
    if isinstance(
        error,
        (
            TypeSafeAuthenticationError,
            TypeSafePermissionDeniedError,
            TypeSafeBadRequestError,
            TypeSafeNotFoundError,
            TypeSafeUnprocessableEntityError,
        ),
    ):
        return False
    if isinstance(error, TypeSafeAPIError):
        return error.status in (408, 429) or error.status >= 500
    return False
