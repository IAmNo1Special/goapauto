"""Characterization tests for the frozen 0.5.0 Jev surface.

These pin the EXACT observable behavior of ``JevSensor`` / ``JevGoalStrategy``
(prompt payloads, exception contracts, client lifecycle, telemetry values)
BEFORE the provider-independent judgment layer lands. They run against the
current non-delegating implementation and must stay green if/when the
internals delegate to ``JudgmentSensor``/``JevJudge`` -- any drift (prompt
change, renamed error, leaked client) fails here first.

Prompt drift is the single biggest regression risk in this program:
Soulscape depends on ``JevSensor.judge`` byte-identical behavior.
"""

import pytest
from typesafe_sdk import Choice, Noul, Score, TypeSafeError

from goapauto.models.goal import Goal
from goapauto.models.jev import JevGoalStrategy, JevSensor
from goapauto.models.worldstate import WorldState
from goapauto.testing import FakeTypeSafeClient


def sdk_questions():
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


def test_jev_sensor_delegation_question_identity():
    """The SDK questions reaching the provider are identical to the frozen path's.

    Asserts on the exact question objects recorded by ``FakeTypeSafeClient.calls``:
    same keys, same objects (identity, not just equality), same state payload.
    Under delegation this pins the ``metadata["jev.question"]`` verbatim round-trip.
    """
    questions = sdk_questions()
    client = FakeTypeSafeClient(
        answers={"danger": 0.5, "threat": "hunting", "hunger": 1.0}
    )
    sensor = JevSensor(
        observe=lambda: {"scene": "woods"}, questions=questions, client=client
    )

    assert sensor.sense() == {"danger": 0.5, "threat": "hunting", "hunger": 1.0}

    assert len(client.calls) == 1
    state, asked = client.calls[0]
    assert state == {"scene": "woods"}
    assert set(asked) == {"danger", "threat", "hunger"}
    for name, original in questions.items():
        assert asked[name] is original
        assert asked[name] == original
        assert asked[name].model_dump() == original.model_dump()


def test_jev_goal_strategy_delegation_prompt_identity():
    """The strategy's Choice question carries the original dict-valued criteria.

    The criteria values must be dicts (``{"target_state": ..., "priority": ...}``),
    never JSON strings -- a JSON round-trip would change the model prompt with
    zero test coverage. Under delegation this pins the metadata round-trip.
    """
    goals = [
        Goal(name="Rest", target_state={"rested": True}),
        Goal(name="Eat", target_state={"fed": True}),
    ]
    client = FakeTypeSafeClient(answers={"goal": "Rest"})
    strategy = JevGoalStrategy(client=client)

    assert strategy.select(goals, WorldState()) is goals[0]

    assert len(client.calls) == 1
    called_state, asked = client.calls[0]
    assert set(asked) == {"goal"}
    question = asked["goal"]
    assert isinstance(question, Choice)
    assert question.instructions == "Which goal should the agent pursue next?"
    assert question.criteria == {
        "Rest": {"target_state": {"rested": True}, "priority": 1},
        "Eat": {"target_state": {"fed": True}, "priority": 1},
    }
    assert all(isinstance(v, dict) for v in question.criteria.values())
    assert called_state == {
        "world_state": WorldState().to_dict(),
        "goals": [
            {"name": "Rest", "target_state": {"rested": True}},
            {"name": "Eat", "target_state": {"fed": True}},
        ],
    }


def test_jev_sensor_delegation_error_name_preserved():
    """Failure telemetry records the SDK error name, not a wrapper's."""
    records = []
    api = FakeTypeSafeClient(
        responder=lambda state, questions: (_ for _ in ()).throw(TypeSafeError("boom"))
    )
    sensor = JevSensor(
        observe=lambda: {},
        questions=sdk_questions(),
        client=api,
        max_stale=0.0,
        telemetry=records.append,
    )

    assert sensor.sense() == {}
    assert records[0].error == "TypeSafeError"
    stats = sensor.stats()
    assert (stats.calls, stats.errors) == (1, 1)


def test_jev_sensor_delegation_construction_failure_closes_client(monkeypatch):
    """Bad mapping raises TypeSafeError and never leaks a created client.

    The frozen contract validates before creating the client; under delegation
    the client may be created before the inner validation fires -- either way,
    no open client may be left behind.
    """
    closed = []

    class SpyClient:
        def __init__(self, *args, **kwargs):
            self.was_closed = False

        def close(self):
            self.was_closed = True
            closed.append(self)

        def system_one(self, *args, **kwargs):
            raise AssertionError("must not be called")

    monkeypatch.setattr("goapauto.models.jev.TypeSafeClient", SpyClient)
    with pytest.raises(TypeSafeError, match="unknown questions"):
        JevSensor(
            observe=lambda: {},
            questions=sdk_questions(),
            mapping={"nope": "danger"},
            client=None,
        )
    assert all(client.was_closed for client in closed)


def test_jev_sensor_double_close_idempotent(mocker):
    """close() twice on a sensor must not raise."""
    mocker.patch("goapauto.models.jev.TypeSafeClient")
    sensor = JevSensor(observe=lambda: {}, questions=sdk_questions())
    sensor.close()
    sensor.close()


def test_jev_goal_strategy_double_close_idempotent(mocker):
    """close() twice on a strategy must not raise."""
    mocker.patch("goapauto.models.jev.TypeSafeClient")
    strategy = JevGoalStrategy()
    strategy.close()
    strategy.close()


def test_jev_sensor_stats_count_every_attempted_call(mocker):
    """calls/errors count every attempted call, on every failure-table row."""
    records = []
    api = mocker.Mock()
    api.system_one.side_effect = [
        _sdk_response(),
        TypeSafeError("boom"),
        TypeSafeError("boom"),
    ]
    sensor = JevSensor(
        observe=lambda: {},
        questions=sdk_questions(),
        client=api,
        max_stale=0.0,
        telemetry=records.append,
    )

    assert sensor.sense() == {"danger": 0.87, "threat": "hunting", "hunger": 2.0}
    assert sensor.sense() == {}
    assert sensor.sense() == {}

    stats = sensor.stats()
    assert stats.calls == 3
    assert stats.errors == 2
    assert stats.stale_cache_hits == 0
    assert [r.error for r in records] == [None, "TypeSafeError", "TypeSafeError"]


def _sdk_response():
    from typesafe_sdk import (
        ChoiceAnswer,
        NoulAnswer,
        ScoreAnswer,
        SystemOneResponse,
        Usage,
    )

    return SystemOneResponse(
        model="jev-latest",
        usage=Usage(input_tokens=10, output_tokens=2),
        answers={
            "danger": NoulAnswer(noul=0.87),
            "threat": ChoiceAnswer(
                choice="hunting",
                confidence=0.9,
                probabilities={"hunting": 0.9, "sleeping": 0.1},
            ),
            "hunger": ScoreAnswer(
                score=2.0,
                confidence=0.8,
                legend={0: "not hungry", 1: "hungry", 2: "starving"},
                probabilities={0: 0.1, 1: 0.2, 2: 0.7},
            ),
        },
    )
