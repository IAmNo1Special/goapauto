"""Tests for JevJudge: the Jev (TypeSafeClient) backend for the judgment layer."""

import httpx2
import pytest
from typesafe_sdk import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    SystemOneResponse,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeBadRequestError,
    TypeSafeError,
    TypeSafeInternalServerError,
    TypeSafeNotFoundError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
    TypeSafeUnprocessableEntityError,
    Usage,
)

from goapauto.models import judgment as core
from goapauto.models.judgment import Judge, JudgmentError
from goapauto.testing import FakeTypeSafeClient


def api_error(cls, status, **kwargs):
    return cls(status=status, body={}, headers=httpx2.Headers(), **kwargs)


def make_judge(**kwargs):
    return FakeTypeSafeClient(answers={}, **kwargs)


def core_questions():
    return {
        "danger": core.NoulQuestion("Is the agent in danger?"),
        "threat": core.ChoiceQuestion(
            "What is the nearest creature doing?",
            choices=("hunting", "sleeping"),
        ),
        "hunger": core.ScoreQuestion(
            "How urgent is food?", min_value=0.0, max_value=2.0
        ),
    }


class TestJevJudgeConstruction:
    def test_backend_is_jev(self):
        from goapauto.models.jev import JevJudge

        assert JevJudge(make_judge()).backend == "jev"

    def test_default_client_timeout(self, mocker):
        from goapauto.models.jev import JevJudge

        client_cls = mocker.patch("goapauto.models.jev.TypeSafeClient")
        JevJudge()
        client_cls.assert_called_once_with(timeout=30.0)

    def test_caller_client_not_closed(self):
        from goapauto.models.jev import JevJudge

        client = FakeTypeSafeClient(answers={})
        closed = []
        client.close = closed.append
        judge = JevJudge(client)
        judge.close()
        assert closed == []

    def test_owned_client_close_idempotent(self, mocker):
        from goapauto.models.jev import JevJudge

        client_cls = mocker.patch("goapauto.models.jev.TypeSafeClient")
        judge = JevJudge()
        judge.close()
        judge.close()
        client_cls.return_value.close.assert_called_once_with()

    def test_satisfies_judge_protocol(self):
        from goapauto.models.jev import JevJudge

        assert isinstance(JevJudge(make_judge()), Judge)


class TestCoreToSdk:
    def test_verbatim_metadata_question_identity(self):
        from goapauto.models.jev import JevJudge

        sdk_question = Choice(
            instructions="Custom?",
            criteria={"a": {"deep": "structure"}},
        )
        questions = {
            "q": core.ChoiceQuestion(
                "Custom?", choices=("a", "b"), metadata={"jev.question": sdk_question}
            )
        }
        client = FakeTypeSafeClient(answers={"q": "a"})
        JevJudge(client).judge({"s": 1}, questions)
        (asked,) = client.calls[0][1].values()
        assert asked is sdk_question

    def test_verbatim_choice_keeps_dict_criteria(self):
        from goapauto.models.jev import JevJudge

        sdk_question = Choice(
            instructions="Which goal?",
            criteria={"Rest": {"target_state": {"rested": True}, "priority": 1}},
        )
        questions = {
            "goal": core.ChoiceQuestion(
                "Which goal?",
                choices=("Rest",),
                metadata={"jev.question": sdk_question},
            )
        }
        client = FakeTypeSafeClient(answers={"goal": "Rest"})
        response = JevJudge(client).judge({}, questions)
        assert response.answers["goal"] == core.ChoiceAnswer("Rest", 1.0)

    def test_mechanical_noul(self):
        from goapauto.models.jev import JevJudge

        client = FakeTypeSafeClient(answers={"danger": 0.5})
        JevJudge(client).judge({}, {"danger": core.NoulQuestion("Is it?")})
        (asked,) = client.calls[0][1].values()
        assert isinstance(asked, Noul)
        assert asked.instructions == "Is it?"

    def test_mechanical_choice_uses_descriptions(self):
        from goapauto.models.jev import JevJudge

        client = FakeTypeSafeClient(answers={"c": "a"})
        JevJudge(client).judge(
            {},
            {
                "c": core.ChoiceQuestion(
                    "Pick?", choices=("a", "b"), descriptions={"a": "first"}
                )
            },
        )
        (asked,) = client.calls[0][1].values()
        assert isinstance(asked, Choice)
        assert asked.criteria == {"a": "first", "b": "b"}

    def test_mechanical_choice_without_descriptions(self):
        from goapauto.models.jev import JevJudge

        client = FakeTypeSafeClient(answers={"c": "b"})
        JevJudge(client).judge(
            {}, {"c": core.ChoiceQuestion("Pick?", choices=("a", "b"))}
        )
        (asked,) = client.calls[0][1].values()
        assert asked.criteria == {"a": "a", "b": "b"}

    def test_mechanical_score_rungs(self):
        from goapauto.models.jev import JevJudge

        client = FakeTypeSafeClient(answers={"hunger": 2.0})
        JevJudge(client).judge(
            {},
            {"hunger": core.ScoreQuestion("How hungry?", min_value=0.0, max_value=2.0)},
        )
        (asked,) = client.calls[0][1].values()
        assert isinstance(asked, Score)
        assert asked.instructions == "How hungry?"
        assert asked.criteria == ["0", "1", "2"]

    def test_mechanical_score_rubric_appended(self):
        from goapauto.models.jev import JevJudge

        client = FakeTypeSafeClient(answers={"hunger": 1.0})
        JevJudge(client).judge(
            {},
            {
                "hunger": core.ScoreQuestion(
                    "How hungry?",
                    min_value=0.0,
                    max_value=2.0,
                    rubric="Eat when starving",
                )
            },
        )
        (asked,) = client.calls[0][1].values()
        assert asked.instructions == "How hungry?\nRubric: Eat when starving"

    def test_mechanical_score_fractional_span(self):
        from goapauto.models.jev import JevJudge

        client = FakeTypeSafeClient(answers={"s": 1.0})
        JevJudge(client).judge(
            {}, {"s": core.ScoreQuestion("?", min_value=0.0, max_value=2.5)}
        )
        (asked,) = client.calls[0][1].values()
        assert asked.criteria == ["0", "1.25", "2.5"]

    def test_mechanical_score_minimum_two_rungs(self):
        from goapauto.models.jev import JevJudge

        client = FakeTypeSafeClient(answers={"s": 0.0})
        JevJudge(client).judge(
            {}, {"s": core.ScoreQuestion("?", min_value=0.0, max_value=0.5)}
        )
        (asked,) = client.calls[0][1].values()
        assert asked.criteria == ["0", "0.5"]


class TestSdkToCore:
    def test_noul_answer_has_no_confidence(self):
        from goapauto.models.jev import JevJudge

        client = FakeTypeSafeClient(answers={"danger": 0.87})
        response = JevJudge(client).judge({}, {"danger": core.NoulQuestion("Is it?")})
        assert response.answers["danger"] == core.FloatAnswer(0.87, None)

    def test_choice_answer_keeps_confidence(self):
        from goapauto.models.jev import JevJudge

        client = FakeTypeSafeClient(answers={"threat": "hunting"})
        response = JevJudge(client).judge(
            {},
            {"threat": core.ChoiceQuestion("What?", choices=("hunting", "sleeping"))},
        )
        assert response.answers["threat"] == core.ChoiceAnswer("hunting", 1.0)

    def test_score_index_maps_linearly(self):
        from goapauto.models.jev import JevJudge

        def responder(state, questions):
            return SystemOneResponse(
                model="jev-latest",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={
                    "hunger": ScoreAnswer(
                        score=1,
                        confidence=0.8,
                        legend={0: "0", 1: "1", 2: "2"},
                        probabilities={0: 0.1, 1: 0.8, 2: 0.1},
                    )
                },
            )

        response = JevJudge(FakeTypeSafeClient(responder=responder)).judge(
            {}, {"hunger": core.ScoreQuestion("?", min_value=0.0, max_value=2.0)}
        )
        assert response.answers["hunger"] == core.FloatAnswer(1.0, 0.8)

    def test_score_index_fractional_interval(self):
        from goapauto.models.jev import JevJudge

        def responder(state, questions):
            (question,) = questions.values()
            return SystemOneResponse(
                model="jev-latest",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={
                    "s": ScoreAnswer(
                        score=2,
                        confidence=0.7,
                        legend={i: label for i, label in enumerate(question.criteria)},
                        probabilities={},
                    )
                },
            )

        response = JevJudge(FakeTypeSafeClient(responder=responder)).judge(
            {}, {"s": core.ScoreQuestion("?", min_value=0.0, max_value=2.5)}
        )
        assert response.answers["s"].value == 2.5

    def test_verbatim_single_rung_score_maps_to_min(self):
        from goapauto.models.jev import JevJudge

        sdk_question = Score(instructions="Only?", criteria=["only"])

        def responder(state, questions):
            return SystemOneResponse(
                model="jev-latest",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={
                    "s": ScoreAnswer(
                        score=0,
                        confidence=0.9,
                        legend={0: "only"},
                        probabilities={0: 1.0},
                    )
                },
            )

        questions = {
            "s": core.ScoreQuestion(
                "Only?",
                min_value=3.0,
                max_value=7.0,
                metadata={"jev.question": sdk_question},
            )
        }
        response = JevJudge(FakeTypeSafeClient(responder=responder)).judge(
            {}, questions
        )
        assert response.answers["s"].value == 3.0

    def test_usage_and_model_mapped(self):
        from goapauto.models.jev import JevJudge

        def responder(state, questions):
            return SystemOneResponse(
                model="jev-latest",
                usage=Usage(input_tokens=10, output_tokens=2),
                answers={"danger": NoulAnswer(noul=0.5)},
            )

        response = JevJudge(FakeTypeSafeClient(responder=responder)).judge(
            {}, {"danger": core.NoulQuestion("Is it?")}
        )
        assert response.model == "jev-latest"
        assert response.usage == core.TokenUsage(input_tokens=10, output_tokens=2)
        assert response.backend == "jev"

    def test_verbatim_non_score_question_has_no_rungs(self):
        from goapauto.models.jev import JevJudge

        def responder(state, questions):
            return SystemOneResponse(
                model="jev-latest",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={
                    "s": ScoreAnswer(
                        score=0,
                        confidence=0.9,
                        legend={0: "a"},
                        probabilities={0: 1.0},
                    )
                },
            )

        questions = {
            "s": core.ScoreQuestion(
                "?",
                min_value=0.0,
                max_value=2.0,
                metadata={"jev.question": Noul(instructions="mismatched")},
            )
        }
        judge = JevJudge(FakeTypeSafeClient(responder=responder))
        with pytest.raises(JudgmentError, match="[Rr]ung index"):
            judge.judge({}, questions)

    def test_empty_questions_rejected(self):
        from goapauto.models.jev import JevJudge

        with pytest.raises(JudgmentError, match="at least one question"):
            JevJudge(make_judge()).judge({}, {})


class TestJevJudgeFailures:
    def _missing(self, name):
        def responder(state, questions):
            return SystemOneResponse(
                model="jev-latest",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={},
            )

        return responder

    def test_missing_noul_answer(self):
        from goapauto.models.jev import JevJudge

        judge = JevJudge(FakeTypeSafeClient(responder=self._missing("danger")))
        with pytest.raises(JudgmentError) as exc_info:
            judge.judge({}, {"danger": core.NoulQuestion("Is it?")})
        error = exc_info.value
        assert error.retryable is False
        assert error.backend == "jev"
        assert isinstance(error.__cause__, TypeSafeError)
        assert "Missing answer for question 'danger'" in str(error.__cause__)

    def test_missing_choice_answer(self):
        from goapauto.models.jev import JevJudge

        judge = JevJudge(FakeTypeSafeClient(responder=self._missing("threat")))
        with pytest.raises(JudgmentError, match="Missing answer"):
            judge.judge(
                {},
                {"threat": core.ChoiceQuestion("What?", choices=("a", "b"))},
            )

    def test_wrong_typed_answer_reads_as_missing(self):
        from goapauto.models.jev import JevJudge

        def responder(state, questions):
            return SystemOneResponse(
                model="jev-latest",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={
                    "danger": ChoiceAnswer(
                        choice="hunting",
                        confidence=1.0,
                        probabilities={"hunting": 1.0},
                    )
                },
            )

        judge = JevJudge(FakeTypeSafeClient(responder=responder))
        with pytest.raises(JudgmentError, match="Missing answer"):
            judge.judge({}, {"danger": core.NoulQuestion("Is it?")})

    def test_no_typesafe_error_escapes(self):
        from goapauto.models.jev import JevJudge

        original = TypeSafeError("boom")

        def responder(state, questions):
            raise original

        judge = JevJudge(FakeTypeSafeClient(responder=responder))
        with pytest.raises(JudgmentError) as exc_info:
            judge.judge({}, {"danger": core.NoulQuestion("Is it?")})
        assert exc_info.value.__cause__ is original
        assert not isinstance(exc_info.value, TypeSafeError)

    def test_require_response_type_error_unchanged(self):
        from goapauto.models.jev import JevJudge

        judge = JevJudge(FakeTypeSafeClient(responder=lambda s, q: {"answers": {}}))
        with pytest.raises(TypeError, match="SystemOneResponse"):
            judge.judge({}, {"danger": core.NoulQuestion("Is it?")})

    def test_raw_exception_unchanged(self):
        from goapauto.models.jev import JevJudge

        boom = RuntimeError("boom")

        def responder(state, questions):
            raise boom

        judge = JevJudge(FakeTypeSafeClient(responder=responder))
        with pytest.raises(RuntimeError) as exc_info:
            judge.judge({}, {"danger": core.NoulQuestion("Is it?")})
        assert exc_info.value is boom

    def test_invalid_noul_value_rejected(self):
        from goapauto.models.jev import JevJudge

        def responder(state, questions):
            return SystemOneResponse(
                model="jev-latest",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={"danger": NoulAnswer(noul=1.5)},
            )

        judge = JevJudge(FakeTypeSafeClient(responder=responder))
        with pytest.raises(JudgmentError) as exc_info:
            judge.judge({}, {"danger": core.NoulQuestion("Is it?")})
        assert exc_info.value.retryable is False
        assert exc_info.value.backend == "jev"
        # Raised directly by the adapter: never coerced through TypeSafeError.
        assert exc_info.value.__cause__ is None

    def test_invalid_confidence_rejected(self):
        from goapauto.models.jev import JevJudge

        def responder(state, questions):
            return SystemOneResponse(
                model="jev-latest",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={
                    "threat": ChoiceAnswer(
                        choice="hunting",
                        confidence=1.5,
                        probabilities={"hunting": 1.0},
                    )
                },
            )

        judge = JevJudge(FakeTypeSafeClient(responder=responder))
        with pytest.raises(JudgmentError, match="[Cc]onfidence") as exc_info:
            judge.judge(
                {},
                {"threat": core.ChoiceQuestion("What?", choices=("hunting",))},
            )
        assert exc_info.value.retryable is False
        assert exc_info.value.backend == "jev"
        # Raised directly by the adapter: never coerced through TypeSafeError.
        assert exc_info.value.__cause__ is None

    def test_score_index_out_of_range_rejected(self):
        from goapauto.models.jev import JevJudge

        def responder(state, questions):
            return SystemOneResponse(
                model="jev-latest",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={
                    "hunger": ScoreAnswer(
                        score=5,
                        confidence=0.9,
                        legend={0: "0", 1: "1", 2: "2"},
                        probabilities={5: 1.0},
                    )
                },
            )

        judge = JevJudge(FakeTypeSafeClient(responder=responder))
        with pytest.raises(JudgmentError, match="[Ss]core") as exc_info:
            judge.judge(
                {},
                {"hunger": core.ScoreQuestion("?", min_value=0.0, max_value=2.0)},
            )
        assert exc_info.value.retryable is False
        assert exc_info.value.backend == "jev"
        assert exc_info.value.__cause__ is None

    def test_fractional_score_in_range_maps_linearly(self):
        from goapauto.models.jev import JevJudge

        def responder(state, questions):
            return SystemOneResponse(
                model="jev-latest",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={
                    "hunger": ScoreAnswer(
                        score=1.5,
                        confidence=0.9,
                        legend={0: "0", 1: "1", 2: "2"},
                        probabilities={1: 1.0},
                    )
                },
            )

        judge = JevJudge(FakeTypeSafeClient(responder=responder))
        response = judge.judge(
            {},
            {"hunger": core.ScoreQuestion("?", min_value=0.0, max_value=2.0)},
        )
        # 0 <= 1.5 <= n-1 == 2, so v = 0 + 1.5 * 2 / 2.
        assert response.answers["hunger"].value == 1.5


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (api_error(TypeSafeRateLimitError, 429, message="slow down"), True),
        (TypeSafeAPITimeoutError(5.0), True),
        (TypeSafeAPIConnectionError("conn failed"), True),
        (api_error(TypeSafeInternalServerError, 500), True),
        (api_error(TypeSafeAPIError, 408), True),
        (api_error(TypeSafeAPIError, 429), True),
        (api_error(TypeSafeAPIError, 503), True),
        (api_error(TypeSafeAuthenticationError, 401), False),
        (api_error(TypeSafePermissionDeniedError, 403), False),
        (api_error(TypeSafeBadRequestError, 400), False),
        (api_error(TypeSafeNotFoundError, 404), False),
        (api_error(TypeSafeUnprocessableEntityError, 422), False),
        (
            TypeSafeAPIResponseValidationError(
                status=422,
                body={},
                headers=httpx2.Headers(),
                field_path="answers",
            ),
            False,
        ),
        (api_error(TypeSafeAPIError, 400), False),
        (api_error(TypeSafeAPIError, 418), False),
        (TypeSafeError("plain"), False),
    ],
)
def test_retry_mapping(error, retryable):
    from goapauto.models.jev import JevJudge

    def responder(state, questions):
        raise error

    judge = JevJudge(FakeTypeSafeClient(responder=responder))
    with pytest.raises(JudgmentError) as exc_info:
        judge.judge({}, {"danger": core.NoulQuestion("Is it?")})
    assert exc_info.value.retryable is retryable
    assert exc_info.value.__cause__ is error
