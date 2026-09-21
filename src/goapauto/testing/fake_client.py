"""Offline stand-in for ``TypeSafeClient`` for tests and ``--demo`` runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

try:
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
except ImportError as exc:
    raise ImportError(
        'goapauto testing fakes require the "jev" extra: uv add "goapauto[jev]"'
    ) from exc

__all__ = ["FakeTypeSafeClient"]


def _answer_for(
    question: Noul | Choice | Score, value: Any
) -> NoulAnswer | ChoiceAnswer | ScoreAnswer:
    if isinstance(value, (NoulAnswer, ChoiceAnswer, ScoreAnswer)):
        return value
    if isinstance(question, Noul):
        return NoulAnswer(noul=float(value))
    if isinstance(question, Choice):
        choice = str(value)
        return ChoiceAnswer(choice=choice, confidence=1.0, probabilities={choice: 1.0})
    score = float(value)
    level = int(score)
    return ScoreAnswer(
        score=score,
        confidence=1.0,
        legend={level: str(level)},
        probabilities={level: 1.0},
    )


class FakeTypeSafeClient:
    """Canned ``system_one`` answers without network or API key.

    Pass a dict of question name to answer value, keyed exactly like the
    questions the caller will ask:

    - ``Noul`` questions take a float in ``[0, 1]``
    - ``Choice`` questions take the chosen label
    - ``Score`` questions take a float (or a pre-built ``ScoreAnswer``)

    Values may also be pre-built ``NoulAnswer``/``ChoiceAnswer``/``ScoreAnswer``
    objects for full control. Alternatively pass ``responder``, a callable
    receiving ``(state, questions)`` and returning a ``SystemOneResponse``,
    for state-dependent answers.

    Every response reports zero token usage. ``close()`` is a no-op.
    """

    def __init__(
        self,
        answers: Mapping[str, Any] | None = None,
        *,
        responder: Callable[[Any, Mapping[str, Any]], SystemOneResponse] | None = None,
        model: str = "fake",
    ) -> None:
        if answers is None and responder is None:
            raise TypeSafeError("FakeTypeSafeClient needs answers or a responder.")
        self._answers = dict(answers) if answers else {}
        self._responder = responder
        self._model = model
        self.calls: list[tuple[Any, Mapping[str, Any]]] = []

    def system_one(
        self, state: Any, questions: Mapping[str, Noul | Choice | Score], **kwargs: Any
    ) -> SystemOneResponse:
        """Return canned answers for the asked questions."""
        self.calls.append((state, dict(questions)))
        if self._responder is not None:
            return self._responder(state, questions)
        built: dict[str, NoulAnswer | ChoiceAnswer | ScoreAnswer] = {}
        for name, question in questions.items():
            if name not in self._answers:
                raise TypeSafeError(
                    f"FakeTypeSafeClient has no canned answer for {name!r}."
                )
            built[name] = _answer_for(question, self._answers[name])
        return SystemOneResponse(
            model=self._model,
            usage=Usage(input_tokens=0, output_tokens=0),
            answers=built,
        )

    def close(self) -> None:
        """No-op; the fake holds no resources."""
