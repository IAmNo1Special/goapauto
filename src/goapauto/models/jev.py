from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from goapauto.models.goal import Goal
from goapauto.models.sensors import Sensor
from goapauto.models.worldstate import WorldState

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"


class TypeSafeError(Exception):
    """Raised when the TypeSafe API cannot be reached or rejects a request."""


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode()[:300]
    except Exception:
        return ""


class TypeSafeClient:
    """Minimal stdlib client for the TypeSafe System One API.

    Sends structured *state* plus named judgment *questions* to
    ``POST /v1/systemone`` and returns the parsed response. ``choice``,
    ``noul`` and ``score`` questions in one call are evaluated in parallel
    by the model, so batching questions barely changes latency.

    Reads ``TYPESAFE_API_KEY`` (required), ``TYPESAFE_BASE_URL`` and
    ``TYPESAFE_DEFAULT_MODEL`` from the environment unless overridden.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY", "")
        if not key.strip():
            raise TypeSafeError(
                "A TypeSafe API key is required: pass api_key or set TYPESAFE_API_KEY."
            )
        self.api_key = key
        self.base_url = (
            base_url or os.environ.get("TYPESAFE_BASE_URL", "") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.model = (
            model or os.environ.get("TYPESAFE_DEFAULT_MODEL", "") or DEFAULT_MODEL
        )
        self.timeout = timeout
        self.max_retries = max_retries

    def system_one(
        self, state: Any, questions: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Ask named judgment questions about state; return the parsed response."""
        return self._request(
            "POST",
            "/v1/systemone",
            {"state": state, "questions": questions, "model": self.model},
        )

    def list_models(self) -> list[dict[str, Any]]:
        """List the models available to the API key."""
        result = self._request("GET", "/v1/models")
        if not isinstance(result, list):
            raise TypeSafeError(f"Unexpected /v1/models response: {result!r}")
        return result

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        last_error: TypeSafeError | None = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                self.base_url + path,
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                detail = _read_error(exc)
                if exc.code == 401:
                    raise TypeSafeError("TypeSafe rejected the API key (401).") from exc
                if exc.code == 429 or 500 <= exc.code < 600:
                    last_error = TypeSafeError(f"TypeSafe HTTP {exc.code}: {detail}")
                else:
                    raise TypeSafeError(f"TypeSafe HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = TypeSafeError(f"TypeSafe connection failed: {exc}")
            if attempt < self.max_retries:
                time.sleep(2**attempt)
        assert last_error is not None
        raise last_error


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
    """

    def __init__(
        self,
        observe: Callable[[], dict[str, Any]],
        questions: dict[str, dict[str, Any]],
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
        self._client = client or TypeSafeClient()
        self._min_interval = min_interval
        self._resense_on_change = resense_on_change
        self._cached: dict[str, Any] = {}
        self._last_observation: dict[str, Any] | None = None
        self._last_call: float = 0.0

    def sense(self) -> dict[str, Any]:
        """Return judgment-derived state updates, re-querying only when due."""
        observation = self._observe()
        if self._should_resense(observation):
            try:
                response = self._client.system_one(observation, self._questions)
                self._cached = self._extract(response["answers"])
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

    def _extract(self, answers: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        for question_name, state_key in self._mapping.items():
            try:
                answer = answers[question_name]
            except KeyError as exc:
                raise TypeSafeError(
                    f"Missing answer for question {question_name!r}"
                ) from exc
            answer_type = answer["type"]
            if answer_type == "noul":
                updates[state_key] = answer["noul"]
            elif answer_type == "choice":
                updates[state_key] = answer["choice"]
            elif answer_type == "score":
                updates[state_key] = answer["score"]
            else:
                raise TypeSafeError(f"Unknown answer type: {answer_type!r}")
        return updates


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
        self._client = client or TypeSafeClient()
        self._instructions = instructions

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
            label: {"target_state": goal.target_state, "priority": goal.priority}
            for label, goal in labeled
        }
        try:
            response = self._client.system_one(
                {
                    "world_state": state.to_dict(),
                    "goals": [
                        {"name": label, "target_state": goal.target_state}
                        for label, goal in labeled
                    ],
                },
                {
                    "goal": {
                        "type": "choice",
                        "instructions": self._instructions,
                        "criteria": criteria,
                    }
                },
            )
            pick = response["answers"]["goal"]["choice"]
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
