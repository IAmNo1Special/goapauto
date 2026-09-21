"""A canned judge for tests. Imports nothing from ``typesafe_sdk``."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from goapauto.models.judgment import (
    Answer,
    JudgmentError,
    JudgmentResponse,
    Question,
)

__all__ = ["FakeJudge"]


class FakeJudge:
    """Answer from canned ``answers`` or a ``responder`` callable.

    ``responder(state, questions)`` returns a plain ``dict[str, Answer]``.
    Records every call in ``calls``. One of ``answers`` or ``responder`` is
    required.
    """

    backend = "fake"

    def __init__(
        self,
        answers: Mapping[str, Answer] | None = None,
        responder: Callable[
            [Mapping[str, Any], Mapping[str, Question]], Mapping[str, Answer]
        ]
        | None = None,
        model: str = "fake",
    ) -> None:
        if answers is None and responder is None:
            raise ValueError("FakeJudge needs answers or responder.")
        self._answers = dict(answers) if answers is not None else None
        self._responder = responder
        self.model = model
        self.calls: list[tuple[Mapping[str, Any], Mapping[str, Question]]] = []

    def judge(
        self, state: Mapping[str, Any], questions: Mapping[str, Question]
    ) -> JudgmentResponse:
        if not questions:
            raise JudgmentError(
                "FakeJudge needs at least one question.",
                backend=self.backend,
                retryable=False,
            )
        if self._responder is not None:
            answers = dict(self._responder(state, questions))
        else:
            assert self._answers is not None
            missing = sorted(set(questions) - set(self._answers))
            if missing:
                raise JudgmentError(
                    f"No canned answer for questions: {missing}.",
                    backend=self.backend,
                    retryable=False,
                )
            answers = {name: self._answers[name] for name in questions}
        self.calls.append((dict(state), dict(questions)))
        return JudgmentResponse(answers=answers, backend=self.backend, model=self.model)

    def close(self) -> None:
        pass
