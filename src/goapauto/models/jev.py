from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from typesafe_sdk import (
    Choice,
    Noul,
    Questions,
    Score,
    SystemOneResponse,
    TypeSafeClient,
    TypeSafeError,
)

from goapauto.models.goal import Goal
from goapauto.models.sensors import Sensor
from goapauto.models.worldstate import WorldState

logger = logging.getLogger(__name__)

__all__ = [
    "JevSensor",
    "JevGoalStrategy",
    "TypeSafeClient",
    "TypeSafeError",
]


# Valid question objects for JevSensor: the official typesafe-sdk classes.
# (The SDK's *Model TypedDicts describe raw-dict shapes, which we reject.)
_QUESTION_TYPES = (Noul, Choice, Score)


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
        client: TypeSafeClient | None = None,
        min_interval: float = 0.0,
        resense_on_change: bool = True,
        max_stale: float = 30.0,
    ) -> None:
        if not questions:
            raise TypeSafeError("JevSensor needs at least one question.")
        if max_stale < 0:
            raise ValueError("max_stale must be non-negative.")
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
        # The SDK's default timeout is 10s; the pre-SDK client used 30s.
        # Keep the long-standing effective default on migration.
        self._client = client if client is not None else TypeSafeClient(timeout=30.0)
        self._owns_client = client is None
        self._min_interval = min_interval
        self._resense_on_change = resense_on_change
        self._max_stale = max_stale
        self._cached: dict[str, Any] = {}
        self._last_observation: dict[str, Any] | None = None
        self._last_call: float = 0.0
        self._last_success: float = 0.0

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
        observation = self._observe()
        if self._should_resense(observation):
            try:
                response = self._client.system_one(observation, self._questions)
                self._cached = self._extract(response)
                self._last_observation = observation
                self._last_call = time.monotonic()
                self._last_success = self._last_call
            except TypeSafeError:
                if time.monotonic() - self._last_success <= self._max_stale:
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
                    return {}
        return dict(self._cached)

    def _should_resense(self, observation: dict[str, Any]) -> bool:
        if self._last_observation is None:
            return True
        if self._resense_on_change and observation != self._last_observation:
            return True
        return (time.monotonic() - self._last_call) >= self._min_interval

    def _extract(self, response: SystemOneResponse) -> dict[str, Any]:
        """Map the SDK's typed answers onto world-state keys.

        Reads through the SDK's own typed accessors (``nouls()``,
        ``choices()``, ``scores()``); the question objects fixed at
        construction decide which accessor each answer comes from.
        """
        response = _require_response(response)
        updates: dict[str, Any] = {}
        for question_name, state_key in self._mapping.items():
            question = self._questions[question_name]
            value: Any
            try:
                if isinstance(question, Noul):
                    value = response.nouls[question_name].noul
                elif isinstance(question, Choice):
                    value = response.choices[question_name].choice
                else:  # Score: the only remaining validated question type.
                    value = response.scores[question_name].score
            except KeyError as exc:
                raise TypeSafeError(
                    f"Missing answer for question {question_name!r}"
                ) from exc
            updates[state_key] = value
        return updates


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
        client: TypeSafeClient | None = None,
        instructions: str = "Which goal should the agent pursue next?",
    ) -> None:
        # See JevSensor: preserve the pre-SDK 30s effective default timeout.
        self._client = client if client is not None else TypeSafeClient(timeout=30.0)
        self._owns_client = client is None
        self._instructions = instructions

    def close(self) -> None:
        """Close the TypeSafe client if this strategy created it.

        Releases the underlying HTTP connection pool. A client passed in
        by the caller is left open; its lifecycle belongs to the caller.
        """
        if self._owns_client:
            self._client.close()

    def select(self, goals: list[Goal], state: WorldState) -> Goal | None:
        """Select the goal Jev judges most worth pursuing right now."""
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
                    {
                        "goal": Choice(
                            instructions=self._instructions,
                            criteria=criteria,
                        )
                    },
                )
            )
            try:
                pick = response.choices["goal"].choice
            except KeyError as exc:
                raise TypeSafeError("Missing answer for question 'goal'") from exc
        except TypeSafeError:
            logger.warning(
                "JevGoalStrategy failed; falling back to first goal", exc_info=True
            )
            return goals[0]
        for label, goal in labeled:
            if label == pick:
                return goal
        logger.warning("Jev chose unknown goal %r; falling back to first goal", pick)
        return goals[0]
