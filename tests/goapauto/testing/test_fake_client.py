"""Tests for goapauto.testing.FakeTypeSafeClient."""

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

from goapauto.testing import FakeTypeSafeClient


def questions():
    return {
        "danger": Noul(instructions="Is the agent in danger?"),
        "move": Choice(
            instructions="Which way?",
            criteria={"left": None, "right": None},
        ),
        "hunger": Score(
            instructions="How hungry?",
            criteria=["full", "hungry", "starving"],
        ),
    }


class TestFakeTypeSafeClient:
    def test_canned_answers_by_type(self):
        client = FakeTypeSafeClient(
            answers={"danger": 0.75, "move": "left", "hunger": 2.0}
        )
        response = client.system_one({"obs": 1}, questions())

        assert isinstance(response, SystemOneResponse)
        assert response.answers["danger"].noul == 0.75
        assert response.answers["move"].choice == "left"
        assert response.answers["hunger"].score == 2.0
        assert response.usage.input_tokens == 0
        assert response.usage.output_tokens == 0

    def test_prebuilt_answers_pass_through(self):
        danger = NoulAnswer(noul=0.1)
        move = ChoiceAnswer(
            choice="right", confidence=0.9, probabilities={"right": 0.9}
        )
        hunger = ScoreAnswer(
            score=1.0,
            confidence=0.8,
            legend={0: "full", 1: "hungry"},
            probabilities={0: 0.2, 1: 0.8},
        )
        client = FakeTypeSafeClient(
            answers={"danger": danger, "move": move, "hunger": hunger}
        )
        response = client.system_one({}, questions())
        assert response.answers["danger"] is danger
        assert response.answers["move"] is move
        assert response.answers["hunger"] is hunger

    def test_missing_answer_raises(self):
        client = FakeTypeSafeClient(answers={"danger": 0.5})
        with pytest.raises(TypeSafeError, match="no canned answer for 'move'"):
            client.system_one({}, questions())

    def test_needs_answers_or_responder(self):
        with pytest.raises(TypeSafeError, match="answers or a responder"):
            FakeTypeSafeClient()

    def test_responder_gets_state_and_questions(self):
        seen = []

        def responder(state, qs):
            seen.append((state, set(qs)))
            return SystemOneResponse(
                model="custom",
                usage=Usage(input_tokens=3, output_tokens=1),
                answers={"danger": NoulAnswer(noul=0.99)},
            )

        client = FakeTypeSafeClient(responder=responder)
        response = client.system_one({"hp": 1}, {"danger": questions()["danger"]})
        assert response.answers["danger"].noul == 0.99
        assert response.model == "custom"
        assert seen == [({"hp": 1}, {"danger"})]

    def test_responder_wins_over_answers(self):
        client = FakeTypeSafeClient(
            answers={"danger": 0.1},
            responder=lambda state, qs: SystemOneResponse(
                model="r",
                usage=Usage(input_tokens=0, output_tokens=0),
                answers={"danger": NoulAnswer(noul=0.2)},
            ),
        )
        response = client.system_one({}, {"danger": questions()["danger"]})
        assert response.answers["danger"].noul == 0.2

    def test_calls_recorded(self):
        client = FakeTypeSafeClient(answers={"danger": 0.5})
        qs = {"danger": questions()["danger"]}
        client.system_one({"a": 1}, qs)
        assert len(client.calls) == 1
        state, asked = client.calls[0]
        assert state == {"a": 1}
        assert set(asked) == {"danger"}

    def test_extra_kwargs_ignored(self):
        client = FakeTypeSafeClient(answers={"danger": 0.5})
        response = client.system_one(
            {}, {"danger": questions()["danger"]}, timeout=5.0, model="x"
        )
        assert response.answers["danger"].noul == 0.5

    def test_close_is_noop(self):
        FakeTypeSafeClient(answers={"danger": 0.5}).close()

    def test_custom_model_name(self):
        client = FakeTypeSafeClient(answers={"danger": 0.5}, model="demo")
        response = client.system_one({}, {"danger": questions()["danger"]})
        assert response.model == "demo"
