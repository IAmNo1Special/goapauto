from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from typesafe_sdk import (
    Choice,
    ChoiceAnswer,
    NoulAnswer,
    Questions,
    ScoreAnswer,
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


def _answers_of(response: SystemOneResponse | dict[str, Any]) -> Mapping[str, Any]:
    """Return the answers mapping from an SDK response or a raw dict payload."""
    if isinstance(response, dict):
        try:
            answers = response["answers"]
        except KeyError as exc:
            raise TypeSafeError("Missing 'answers' in TypeSafe response.") from exc
        if not isinstance(answers, dict):
            raise TypeSafeError("Invalid 'answers' in TypeSafe response.")
        return answers
    return response.answers


def _value_of(answer: Any) -> Any:
    """Extract the plain value from an SDK answer or a raw dict answer."""
    if isinstance(answer, NoulAnswer):
        return answer.noul
    if isinstance(answer, ChoiceAnswer):
        return answer.choice
    if isinstance(answer, ScoreAnswer):
        return answer.score
    if isinstance(answer, dict):
        answer_type = answer.get("type")
        if answer_type == "noul":
            return answer["noul"]
        if answer_type == "choice":
            return answer["choice"]
        if answer_type == "score":
            return answer["score"]
        raise TypeSafeError(f"Unknown answer type: {answer_type!r}")
    raise TypeSafeError(f"Unknown answer type: {type(answer).__name__!r}")


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
    the agent loop keeps running.

    Questions use the official ``typesafe-sdk`` types (``Noul``, ``Choice``,
    ``Score``) or raw question dictionaries; the client is the SDK's
    ``TypeSafeClient`` directly.
    """

    def __init__(
        self,
        observe: Callable[[], dict[str, Any]],
        questions: Questions,
        mapping: dict[str, str] | None = None,
        client: TypeSafeClient | None = None,
        min_interval: float = 0.0,
        resense_on_change: bool = True,
    ) -> None:
        if not questions:
            raise TypeSafeError("JevSensor needs at least one question.")
        self._observe = observe
        self._questions = questions
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
        self._cached: dict[str, Any] = {}
        self._last_observation: dict[str, Any] | None = None
        self._last_call: float = 0.0

    def close(self) -> None:
        """Close the TypeSafe client if this sensor created it.

        Releases the underlying HTTP connection pool. A client passed in
        by the caller is left open; its lifecycle belongs to the caller.
        """
        if self._owns_client:
            self._client.close()

    def sense(self) -> dict[str, Any]:
        """Return judgment-derived state updates, re-querying only when due."""
        observation = self._observe()
        if self._should_resense(observation):
            try:
                response = self._client.system_one(observation, self._questions)
                self._cached = self._extract(_answers_of(response))
                self._last_observation = observation
                self._last_call = time.monotonic()
            except TypeSafeError:
                logger.warning(
                    "JevSensor failed; reusing last judgments", exc_info=True
                )
        return dict(self._cached)

    def _should_resense(self, observation: dict[str, Any]) -> bool:
        if self._last_observation is None:
            return True
        if self._resense_on_change and observation != self._last_observation:
            return True
        return (time.monotonic() - self._last_call) >= self._min_interval

    def _extract(self, answers: Mapping[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        for question_name, state_key in self._mapping.items():
            try:
                answer = answers[question_name]
            except KeyError as exc:
                raise TypeSafeError(
                    f"Missing answer for question {question_name!r}"
                ) from exc
            updates[state_key] = _value_of(answer)
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
            response = self._client.system_one(
                {
                    "world_state": state.to_dict(),
                    "goals": [
                        {"name": label, "target_state": _json_safe(goal.target_state)}
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
            answers = _answers_of(response)
            try:
                goal_answer = answers["goal"]
            except KeyError as exc:
                raise TypeSafeError("Missing answer for question 'goal'") from exc
            pick = _value_of(goal_answer)
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
