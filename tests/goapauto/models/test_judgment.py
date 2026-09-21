"""Tests for the provider-independent judgment layer (design 05).

The sensor/strategy suites are parametrized across three judge backends --
``JevJudge`` (over ``FakeTypeSafeClient``), ``FakeJudge``, and the test-only
``RuleJudge`` -- so the generic layers are proven backend-agnostic.
"""

import dataclasses
import logging
import subprocess
import sys
from typing import Any

import pytest

from goapauto import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    FloatAnswer,
    Judge,
    JudgmentCallRecord,
    JudgmentError,
    JudgmentGoalStrategy,
    JudgmentResponse,
    JudgmentSensor,
    JudgmentStats,
    NoulQuestion,
    Question,
    ScoreQuestion,
    TokenUsage,
)
from goapauto.models.goal import Goal
from goapauto.models.judgment import _json_safe
from goapauto.models.worldstate import WorldState
from goapauto.testing import FakeJudge

# ---------------------------------------------------------------------------
# Test-only judge: alien keyword-based internal representation.
# ---------------------------------------------------------------------------


class RuleJudge:
    """Answers from keyword hits in a rendered state text.

    Deliberately unlike any provider: no confidences on Noul answers, no
    probabilities, and a Choice that fails closed when no label matches.
    """

    backend = "rule"

    def __init__(self, keywords):
        self._keywords = keywords
        self.calls = []

    def judge(self, state, questions):
        if not questions:
            raise JudgmentError(
                "RuleJudge needs at least one question.",
                backend=self.backend,
                retryable=False,
            )
        text = self._render(state)
        answers = {}
        for name, question in questions.items():
            spec = self._keywords.get(name, {})
            if isinstance(question, NoulQuestion):
                answers[name] = FloatAnswer(
                    1.0 if any(k in text for k in spec.get("yes", ())) else 0.0
                )
            elif isinstance(question, ChoiceQuestion):
                for choice in question.choices:
                    if choice.lower() in text:
                        answers[name] = ChoiceAnswer(choice)
                        break
                else:
                    raise JudgmentError(
                        f"No choice matched the state for question {name!r}.",
                        backend=self.backend,
                        retryable=False,
                    )
            elif isinstance(question, ScoreQuestion):
                hits = sum(text.count(k) for k in spec.get("count", ()))
                idx = min(hits, 4)
                value = (
                    question.min_value
                    + (question.max_value - question.min_value) * idx / 4
                )
                answers[name] = FloatAnswer(value)
        self.calls.append((dict(state), dict(questions)))
        return JudgmentResponse(answers=answers, backend=self.backend, model="rule-1")

    def _render(self, state):
        parts = []

        def walk(value):
            if isinstance(value, dict):
                for key, sub in value.items():
                    parts.append(str(key))
                    walk(sub)
            elif isinstance(value, (list, tuple)):
                for sub in value:
                    walk(sub)
            else:
                parts.append(str(value))

        walk(state)
        return " ".join(parts).lower()

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Backend arms for the parametrized suites.
# ---------------------------------------------------------------------------


class Arm:
    """One judge backend under test."""

    def __init__(
        self, name, make_judge, obs_a, obs_b, expected_a, expected_b, strategy_pick
    ):
        self.name = name
        self.make_judge = make_judge
        self.obs_a = obs_a
        self.obs_b = obs_b
        self.expected_a = expected_a
        self.expected_b = expected_b
        self.strategy_pick = strategy_pick


def _jev_sensor_arm():
    from goapauto.models.jev import JevJudge
    from goapauto.testing import FakeTypeSafeClient

    client = FakeTypeSafeClient(
        answers={"danger": 0.87, "threat": "hunting", "hunger": 2.0}
    )
    return JevJudge(client), client.calls


def _jev_strategy_judge(pick):
    from goapauto.models.jev import JevJudge
    from goapauto.testing import FakeTypeSafeClient

    return JevJudge(FakeTypeSafeClient(answers={"goal": pick}))


def _fake_sensor_arm():
    judge = FakeJudge(
        answers={
            "danger": FloatAnswer(0.87),
            "threat": ChoiceAnswer("hunting", 0.9),
            "hunger": FloatAnswer(2.0, 0.8),
        }
    )
    return judge, judge.calls


def _fake_strategy_judge(pick):
    return FakeJudge(answers={"goal": ChoiceAnswer(pick)})


def _rule_sensor_arm():
    judge = RuleJudge(
        {
            "danger": {"yes": ("danger", "hostile")},
            "hunger": {"count": ("hungry",)},
        }
    )
    return judge, judge.calls


def _rule_strategy_judge(pick):
    return RuleJudge({})


ARMS = {
    "jev": Arm(
        name="jev",
        make_judge=_jev_sensor_arm,
        obs_a={"tick": 1},
        obs_b={"tick": 2},
        expected_a={"danger": 0.87, "threat": "hunting", "hunger": 2.0},
        expected_b={"danger": 0.87, "threat": "hunting", "hunger": 2.0},
        strategy_pick="Sleep",
    ),
    "fake": Arm(
        name="fake",
        make_judge=_fake_sensor_arm,
        obs_a={"tick": 1},
        obs_b={"tick": 2},
        expected_a={"danger": 0.87, "threat": "hunting", "hunger": 2.0},
        expected_b={"danger": 0.87, "threat": "hunting", "hunger": 2.0},
        strategy_pick="Sleep",
    ),
    "rule": Arm(
        name="rule",
        make_judge=_rule_sensor_arm,
        obs_a={
            "scene": "danger: hostile creature hunting, hungry hungry hungry hungry"
        },
        obs_b={"scene": "a sleeping creature"},
        expected_a={"danger": 1.0, "threat": "hunting", "hunger": 2.0},
        expected_b={"danger": 0.0, "threat": "sleeping", "hunger": 0.0},
        strategy_pick="Eat",
    ),
}

STRATEGY_JUDGES = {
    "jev": _jev_strategy_judge,
    "fake": _fake_strategy_judge,
    "rule": _rule_strategy_judge,
}


@pytest.fixture(params=sorted(ARMS))
def arm(request):
    return ARMS[request.param]


def core_questions(**overrides):
    questions = {
        "danger": NoulQuestion("Is the agent in danger?"),
        "threat": ChoiceQuestion(
            "What is the nearest creature doing?",
            choices=("hunting", "sleeping"),
        ),
        "hunger": ScoreQuestion("How urgent is food?", min_value=0.0, max_value=2.0),
    }
    questions.update(overrides)
    return questions


def make_sensor(arm, **kwargs):
    judge, calls = arm.make_judge()
    kwargs.setdefault("observe", lambda: arm.obs_a)
    kwargs.setdefault("questions", core_questions())
    return JudgmentSensor(judge, **kwargs), calls


# ---------------------------------------------------------------------------
# Core types.
# ---------------------------------------------------------------------------


class TestCoreTypes:
    def test_question_defaults_and_immutability(self):
        noul = NoulQuestion("Danger?")
        assert noul.metadata is None
        choice = ChoiceQuestion("Pick?", choices=["a", "b"])
        assert choice.descriptions is None
        assert choice.metadata is None
        score = ScoreQuestion("How much?")
        assert (score.min_value, score.max_value) == (0.0, 1.0)
        assert score.rubric is None
        for frozen in (noul, choice, score):
            with pytest.raises(dataclasses.FrozenInstanceError):
                frozen.instructions = "changed"

    def test_answer_defaults_and_immutability(self):
        assert FloatAnswer(0.5).confidence is None
        assert ChoiceAnswer("a").confidence is None
        for frozen in (FloatAnswer(0.5), ChoiceAnswer("a")):
            with pytest.raises(dataclasses.FrozenInstanceError):
                frozen.value = "changed"

    def test_response_and_usage_defaults(self):
        usage = TokenUsage()
        assert (usage.input_tokens, usage.output_tokens) == (None, None)
        response = JudgmentResponse(answers={}, backend="fake")
        assert response.model is None
        assert response.latency_ms is None
        assert response.usage is None

    def test_union_aliases_exist(self):
        assert Question is not None
        assert Answer is not None

    def test_choice_requires_nonempty_unique_labels(self):
        with pytest.raises(ValueError, match="non-empty"):
            ChoiceQuestion("Pick?", choices=[])
        with pytest.raises(ValueError, match="non-empty"):
            ChoiceQuestion("Pick?", choices=["", "a"])
        with pytest.raises(ValueError, match="unique"):
            ChoiceQuestion("Pick?", choices=["a", "a"])

    def test_score_requires_ordered_interval(self):
        with pytest.raises(ValueError, match="min_value"):
            ScoreQuestion("How much?", min_value=1.0, max_value=1.0)
        with pytest.raises(ValueError, match="min_value"):
            ScoreQuestion("How much?", min_value=2.0, max_value=1.0)

    def test_json_safe_branches(self):
        assert _json_safe(None) is None
        assert _json_safe("x") == "x"
        assert _json_safe(3) == 3
        assert _json_safe(True) is True
        assert _json_safe({"a": 1}) == {"a": 1}
        assert _json_safe([1, (2,)]) == [1, [2]]
        assert _json_safe(len) == "len"
        assert _json_safe(object()).startswith("<")


class TestJudgmentError:
    def test_retryable_is_required(self):
        error = JudgmentError("bad", backend="fake", retryable=True)
        assert str(error) == "bad"
        assert error.backend == "fake"
        assert error.retryable is True
        with pytest.raises(TypeError):
            JudgmentError("bad", backend="fake")
        with pytest.raises(TypeError):
            JudgmentError("bad", "fake", True)

    def test_stats_and_record_shapes(self):
        stats = JudgmentStats()
        assert (stats.calls, stats.errors, stats.stale_cache_hits) == (0, 0, 0)
        record = JudgmentCallRecord(source="sensor", backend="fake", latency_ms=1.5)
        assert record.error is None
        assert record.stale_cache_hit is False


class TestJudgeProtocol:
    def test_runtime_checkable(self):
        assert isinstance(FakeJudge(answers={}), Judge)
        assert isinstance(RuleJudge({}), Judge)
        assert not isinstance(object(), Judge)

    def test_backend_name_falls_back_to_unknown(self):
        class BareJudge:
            def judge(self, state, questions):
                raise AssertionError("not called")

            def close(self):
                pass

        sensor = JudgmentSensor(
            BareJudge(), observe=lambda: {}, questions=core_questions()
        )
        assert sensor.backend_name() == "unknown"


# ---------------------------------------------------------------------------
# FakeJudge.
# ---------------------------------------------------------------------------


class TestFakeJudge:
    def test_backend_and_model(self):
        judge = FakeJudge(answers={"q": FloatAnswer(0.5)})
        assert judge.backend == "fake"
        assert FakeJudge(answers={}, model="m-1").model == "m-1"

    def test_canned_answers(self):
        answers = {"danger": FloatAnswer(0.5)}
        judge = FakeJudge(answers=answers)
        response = judge.judge({"s": 1}, {"danger": NoulQuestion("?")})
        assert response.answers == answers
        assert response.backend == "fake"

    def test_responder_receives_state_and_questions(self):
        seen = {}

        def responder(state, questions):
            seen["state"] = state
            seen["questions"] = questions
            return {"q": FloatAnswer(1.0)}

        judge = FakeJudge(responder=responder)
        questions = {"q": NoulQuestion("?")}
        judge.judge({"s": 2}, questions)
        assert seen["state"] == {"s": 2}
        assert seen["questions"] == questions

    def test_needs_answers_or_responder(self):
        with pytest.raises(ValueError, match="answers or responder"):
            FakeJudge()

    def test_empty_questions_rejected(self):
        with pytest.raises(JudgmentError, match="at least one question"):
            FakeJudge(answers={}).judge({}, {})

    def test_missing_canned_answer(self):
        judge = FakeJudge(answers={})
        with pytest.raises(JudgmentError, match="No canned answer"):
            judge.judge({}, {"q": NoulQuestion("?")})

    def test_close_and_calls(self):
        judge = FakeJudge(answers={"q": FloatAnswer(0.5)})
        judge.judge({}, {"q": NoulQuestion("?")})
        assert len(judge.calls) == 1
        judge.close()

    def test_import_does_not_load_typesafe_sdk(self):
        code = (
            "import sys; "
            "from goapauto.testing.fake_judge import FakeJudge; "
            "assert 'typesafe_sdk' not in sys.modules, 'typesafe_sdk leaked'; "
            "assert FakeJudge.backend == 'fake'; "
            "print('ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"


class TestTestingLazyExports:
    def test_lazy_branches(self):
        import goapauto.testing as testing

        assert testing.FakeJudge is FakeJudge
        from goapauto.testing import FakeTypeSafeClient

        assert testing.FakeTypeSafeClient is FakeTypeSafeClient

    def test_unknown_attribute(self):
        import goapauto.testing as testing

        with pytest.raises(AttributeError, match="no attribute"):
            _ = testing.does_not_exist


# ---------------------------------------------------------------------------
# RuleJudge.
# ---------------------------------------------------------------------------


class TestRuleJudge:
    def test_backend(self):
        assert RuleJudge({}).backend == "rule"

    def test_noul_yes_no(self):
        judge = RuleJudge({"danger": {"yes": ("danger",)}})
        response = judge.judge({"t": "danger!"}, {"danger": NoulQuestion("?")})
        assert response.answers["danger"].value == 1.0
        assert response.answers["danger"].confidence is None
        response = judge.judge({"t": "calm"}, {"danger": NoulQuestion("?")})
        assert response.answers["danger"].value == 0.0

    def test_choice_first_match(self):
        judge = RuleJudge({})
        question = ChoiceQuestion("?", choices=("hunting", "sleeping"))
        response = judge.judge({"t": "it is sleeping"}, {"c": question})
        assert response.answers["c"].value == "sleeping"

    def test_choice_no_match_fails_closed(self):
        judge = RuleJudge({})
        question = ChoiceQuestion("?", choices=("hunting", "sleeping"))
        with pytest.raises(JudgmentError) as exc_info:
            judge.judge({"t": "nothing relevant"}, {"c": question})
        assert exc_info.value.retryable is False
        assert exc_info.value.backend == "rule"

    def test_score_bins(self):
        judge = RuleJudge({"h": {"count": ("hungry",)}})
        question = ScoreQuestion("?", min_value=0.0, max_value=2.0)
        assert judge.judge({"t": ""}, {"h": question}).answers["h"].value == 0.0
        assert (
            judge.judge({"t": "hungry hungry"}, {"h": question}).answers["h"].value
            == 1.0
        )
        assert (
            judge.judge({"t": "hungry " * 9}, {"h": question}).answers["h"].value == 2.0
        )

    def test_empty_questions_rejected(self):
        with pytest.raises(JudgmentError, match="at least one question"):
            RuleJudge({}).judge({}, {})

    def test_calls_recorded(self):
        judge = RuleJudge({})
        judge.judge({"s": 1}, {"q": NoulQuestion("?")})
        assert judge.calls[0][0] == {"s": 1}


# ---------------------------------------------------------------------------
# JudgmentSensor: construction.
# ---------------------------------------------------------------------------


class TestJudgmentSensorConstruction:
    def test_judge_is_required(self):
        with pytest.raises(TypeError, match="judge"):
            JudgmentSensor(None, observe=lambda: {}, questions=core_questions())

    def test_empty_questions_rejected(self):
        with pytest.raises(ValueError, match="at least one question"):
            JudgmentSensor(FakeJudge(answers={}), observe=lambda: {}, questions={})

    def test_unknown_question_type_rejected(self):
        with pytest.raises(TypeError, match="unknown type"):
            JudgmentSensor(
                FakeJudge(answers={}),
                observe=lambda: {},
                questions={"weird": object()},
            )

    def test_unknown_mapping_key_rejected(self):
        with pytest.raises(ValueError, match="unknown questions"):
            JudgmentSensor(
                FakeJudge(answers={}),
                observe=lambda: {},
                questions=core_questions(),
                mapping={"nope": "danger"},
            )

    def test_negative_max_stale_rejected(self):
        with pytest.raises(ValueError, match="max_stale"):
            JudgmentSensor(
                FakeJudge(answers={}),
                observe=lambda: {},
                questions=core_questions(),
                max_stale=-1.0,
            )

    def test_defaults(self):
        judge = FakeJudge(answers={})
        sensor = JudgmentSensor(judge, observe=lambda: {}, questions=core_questions())
        assert sensor.backend_name() == "fake"
        sensor.close()


# ---------------------------------------------------------------------------
# JudgmentSensor: sense/cache behavior, parametrized over backends.
# ---------------------------------------------------------------------------


class TestJudgmentSensorBehavior:
    def test_first_sense_judges(self, arm):
        sensor, calls = make_sensor(arm)
        assert sensor.sense() == arm.expected_a
        assert len(calls) == 1

    def test_min_interval_zero_rejudges_every_call(self, arm):
        sensor, calls = make_sensor(arm, min_interval=0.0)
        sensor.sense()
        assert sensor.sense() == arm.expected_a
        assert len(calls) == 2

    def test_min_interval_positive_serves_cache(self, arm):
        sensor, calls = make_sensor(arm, min_interval=60.0)
        sensor.sense()
        assert sensor.sense() == arm.expected_a
        assert len(calls) == 1

    def test_changed_observation_rejudges(self, arm):
        sensor, calls = make_sensor(arm)
        assert sensor.sense() == arm.expected_a
        assert sensor.judge(arm.obs_b) == arm.expected_b
        assert len(calls) == 2

    def test_changed_observation_rejudges_via_sense(self, arm):
        judge, calls = arm.make_judge()
        observations = [arm.obs_a, arm.obs_b]
        sensor = JudgmentSensor(
            judge,
            observe=lambda: observations.pop(0),
            questions=core_questions(),
            resense_on_change=True,
        )
        assert sensor.sense() == arm.expected_a
        assert sensor.sense() == arm.expected_b
        assert len(calls) == 2

    def test_sense_and_judge_share_cache(self, arm):
        judge, calls = arm.make_judge()
        sensor = JudgmentSensor(
            judge,
            observe=lambda: arm.obs_b,
            questions=core_questions(),
            min_interval=60.0,
        )
        assert sensor.judge(arm.obs_b) == arm.expected_b
        assert sensor.sense() == arm.expected_b
        assert len(calls) == 1

    def test_cache_updates_atomically_after_success(self, arm):
        sensor, calls = make_sensor(arm, min_interval=60.0)
        sensor.sense()
        sensor.sense()
        assert len(calls) == 1

    def test_mapping_renames_state_keys(self, arm):
        mapping = {"danger": "d", "threat": "t", "hunger": "h"}
        sensor, _ = make_sensor(arm, mapping=mapping)
        assert sensor.sense() == {
            "d": arm.expected_a["danger"],
            "t": arm.expected_a["threat"],
            "h": arm.expected_a["hunger"],
        }

    def test_partial_mapping_yields_partial_output(self, arm):
        sensor, _ = make_sensor(arm, mapping={"danger": "d"})
        assert sensor.sense() == {"d": arm.expected_a["danger"]}

    def test_metadata_reaches_judge_untouched(self, arm):
        from typesafe_sdk import Noul as SdkNoul

        verbatim = SdkNoul(instructions="verbatim danger question")
        questions = core_questions(
            danger=NoulQuestion(
                "Is the agent in danger?",
                metadata={"jev.question": verbatim},
            )
        )
        judge, _ = arm.make_judge()
        seen: dict[str, Any] = {}
        original = judge.judge

        def spy(state, asked):
            seen["questions"] = asked
            return original(state, asked)

        judge.judge = spy  # type: ignore[method-assign]
        sensor = JudgmentSensor(judge, observe=lambda: arm.obs_a, questions=questions)
        sensor.sense()
        assert seen["questions"]["danger"].metadata["jev.question"] is verbatim

    def test_state_passed_as_defensive_copy(self, arm):
        seen = []

        def observe():
            return dict(arm.obs_a)

        judge, _ = arm.make_judge()
        original_judge = judge.judge

        def spy_judge(state, questions):
            seen.append(state)
            return original_judge(state, questions)

        judge.judge = spy_judge
        observation = dict(arm.obs_a)
        sensor = JudgmentSensor(
            judge, observe=lambda: observation, questions=core_questions()
        )
        sensor.judge(observation)
        assert seen[0] == observation
        assert seen[0] is not observation

    def test_telemetry_record_shape(self, arm):
        records = []
        sensor, _ = make_sensor(arm, telemetry=records.append)
        sensor.sense()
        (record,) = records
        assert record.source == "sensor"
        assert record.backend == arm.name
        assert record.error is None
        assert record.latency_ms >= 0.0
        assert record.stale_cache_hit is False

    def test_telemetry_copies_usage_fields(self):
        response = JudgmentResponse(
            answers={"danger": FloatAnswer(0.5)},
            backend="fake",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )

        class UsageJudge:
            backend = "fake"

            def judge(self, state, questions):
                return response

            def close(self):
                pass

        records = []
        sensor = JudgmentSensor(
            UsageJudge(),
            observe=lambda: {},
            questions={"danger": NoulQuestion("?")},
            telemetry=records.append,
        )
        sensor.sense()
        assert (records[0].input_tokens, records[0].output_tokens) == (10, 20)

    def test_telemetry_usage_passthrough(self, arm):
        records = []
        sensor, _ = make_sensor(arm, telemetry=records.append)
        sensor.sense()
        if arm.name == "jev":
            # FakeTypeSafeClient reports zero token usage end to end.
            assert (records[0].input_tokens, records[0].output_tokens) == (0, 0)
        else:
            assert records[0].input_tokens is None
            assert records[0].output_tokens is None

    def test_close_delegates_to_judge(self, arm):
        closed = []

        class ClosingJudge:
            backend = arm.name

            def judge(self, state, questions):
                raise AssertionError("not called")

            def close(self):
                closed.append(True)

        sensor = JudgmentSensor(
            ClosingJudge(), observe=lambda: {}, questions=core_questions()
        )
        sensor.close()
        assert closed == [True]


# ---------------------------------------------------------------------------
# JudgmentSensor: answer validation (backend-agnostic).
# ---------------------------------------------------------------------------


class TestJudgmentSensorValidation:
    def _sensor_with(self, answers, **kwargs):
        judge = FakeJudge(answers=answers)
        kwargs.setdefault("observe", lambda: {})
        kwargs.setdefault("questions", core_questions())
        return JudgmentSensor(judge, **kwargs)

    def test_missing_answer_is_fatal(self):
        judge = FakeJudge(
            responder=lambda state, questions: {"danger": FloatAnswer(0.5)}
        )
        sensor = JudgmentSensor(judge, observe=lambda: {}, questions=core_questions())
        with pytest.raises(JudgmentError, match="Missing answers") as exc_info:
            sensor.sense()
        assert exc_info.value.retryable is False

    def test_wrong_answer_type_for_choice_is_fatal(self):
        questions = core_questions(threat=ChoiceQuestion("?", choices=("a", "b")))
        judge = FakeJudge(
            responder=lambda state, questions: {
                "danger": FloatAnswer(0.5),
                "threat": FloatAnswer(0.5),
                "hunger": FloatAnswer(0.5),
            }
        )
        sensor = JudgmentSensor(judge, observe=lambda: {}, questions=questions)
        with pytest.raises(JudgmentError, match="Wrong answer type") as exc_info:
            sensor.sense()
        assert exc_info.value.retryable is False

    def test_extra_answer_keys_ignored_with_warning(self, caplog):
        def responder(state, questions):
            return {
                "danger": FloatAnswer(0.5),
                "threat": ChoiceAnswer("hunting"),
                "hunger": FloatAnswer(1.0),
                "stowaway": FloatAnswer(0.1),
            }

        sensor = JudgmentSensor(
            FakeJudge(responder=responder),
            observe=lambda: {},
            questions=core_questions(),
        )
        with caplog.at_level(logging.WARNING, logger="goapauto.models.judgment"):
            assert sensor.sense() == {
                "danger": 0.5,
                "threat": "hunting",
                "hunger": 1.0,
            }
        assert "stowaway" in caplog.text

    def test_wrong_answer_type_rejected(self):
        sensor = self._sensor_with(
            {
                "danger": ChoiceAnswer("hunting"),
                "threat": ChoiceAnswer("hunting"),
                "hunger": FloatAnswer(1.0),
            }
        )
        with pytest.raises(JudgmentError, match="Wrong answer type"):
            sensor.sense()

    def test_noul_range_enforced(self):
        for bad in (-0.1, 1.1):
            sensor = self._sensor_with(
                {
                    "danger": FloatAnswer(bad),
                    "threat": ChoiceAnswer("hunting"),
                    "hunger": FloatAnswer(1.0),
                }
            )
            with pytest.raises(JudgmentError, match="out of range"):
                sensor.sense()

    def test_score_range_enforced(self):
        sensor = self._sensor_with(
            {
                "danger": FloatAnswer(0.5),
                "threat": ChoiceAnswer("hunting"),
                "hunger": FloatAnswer(2.5),
            }
        )
        with pytest.raises(JudgmentError, match="out of range"):
            sensor.sense()

    def test_choice_membership_enforced(self):
        sensor = self._sensor_with(
            {
                "danger": FloatAnswer(0.5),
                "threat": ChoiceAnswer("dancing"),
                "hunger": FloatAnswer(1.0),
            }
        )
        with pytest.raises(JudgmentError, match="not in choices"):
            sensor.sense()

    @pytest.mark.parametrize("bad_confidence", [-0.1, 1.1, float("nan")])
    def test_confidence_range_enforced(self, bad_confidence):
        sensor = self._sensor_with(
            {
                "danger": FloatAnswer(0.5, bad_confidence),
                "threat": ChoiceAnswer("hunting"),
                "hunger": FloatAnswer(1.0),
            }
        )
        with pytest.raises(JudgmentError, match="[Cc]onfidence"):
            sensor.sense()

    def test_non_numeric_value_rejected(self):
        sensor = self._sensor_with(
            {
                "danger": FloatAnswer("high"),
                "threat": ChoiceAnswer("hunting"),
                "hunger": FloatAnswer(1.0),
            }
        )
        with pytest.raises(JudgmentError, match="not a number"):
            sensor.sense()


# ---------------------------------------------------------------------------
# JudgmentSensor: error policy.
# ---------------------------------------------------------------------------


def _responder_judge(arm_name, error):
    def responder(state, questions):
        raise error

    if arm_name == "jev":
        from goapauto.models.jev import JevJudge
        from goapauto.testing import FakeTypeSafeClient

        return JevJudge(FakeTypeSafeClient(responder=responder))
    return FakeJudge(responder=responder)


class TestJudgmentSensorErrors:
    @pytest.mark.parametrize("arm_name", ["jev", "fake"])
    def test_retryable_error_reuses_fresh_cache(self, arm_name, caplog):
        arm = ARMS[arm_name]
        records = []
        judge = _responder_judge(
            arm_name, JudgmentError("flaky", backend=arm_name, retryable=True)
        )
        sensor = JudgmentSensor(
            judge,
            observe=lambda: arm.obs_a,
            questions=core_questions(),
            max_stale=30.0,
            min_interval=0.0,
            telemetry=records.append,
        )
        # Prime the cache with a working judge first.
        working, _ = arm.make_judge()
        sensor._judge = working
        sensor.sense()
        sensor._judge = judge
        with caplog.at_level(logging.WARNING, logger="goapauto.models.judgment"):
            assert sensor.sense() == arm.expected_a
        assert "retryable" in caplog.text
        stats = sensor.stats()
        assert (stats.calls, stats.errors, stats.stale_cache_hits) == (2, 1, 1)
        assert records[-1].stale_cache_hit is True

    @pytest.mark.parametrize("arm_name", ["jev", "fake"])
    def test_retryable_error_with_dead_cache_returns_empty(self, arm_name, caplog):
        records = []
        judge = _responder_judge(
            arm_name, JudgmentError("flaky", backend=arm_name, retryable=True)
        )
        sensor = JudgmentSensor(
            judge,
            observe=lambda: {},
            questions=core_questions(),
            max_stale=0.0,
            telemetry=records.append,
        )
        with caplog.at_level(logging.ERROR, logger="goapauto.models.judgment"):
            assert sensor.sense() == {}
        assert records[0].error == "JudgmentError"
        stats = sensor.stats()
        assert (stats.calls, stats.errors) == (1, 1)

    @pytest.mark.parametrize("arm_name", ["jev", "fake"])
    def test_non_retryable_propagates_when_fail_loud(self, arm_name):
        records = []
        failure = JudgmentError("bad contract", backend=arm_name, retryable=False)
        judge = _responder_judge(arm_name, failure)
        sensor = JudgmentSensor(
            judge,
            observe=lambda: {},
            questions=core_questions(),
            telemetry=records.append,
        )
        with pytest.raises(JudgmentError) as exc_info:
            sensor.sense()
        assert exc_info.value is failure
        assert records[0].error == "JudgmentError"
        stats = sensor.stats()
        assert (stats.calls, stats.errors) == (1, 1)

    @pytest.mark.parametrize("arm_name", ["jev", "fake"])
    def test_non_retryable_absorbed_when_fail_quiet(self, arm_name, caplog):
        judge = _responder_judge(
            arm_name, JudgmentError("bad contract", backend=arm_name, retryable=False)
        )
        sensor = JudgmentSensor(
            judge,
            observe=lambda: {},
            questions=core_questions(),
            max_stale=30.0,
            fail_loud=False,
        )
        with caplog.at_level(logging.ERROR, logger="goapauto.models.judgment"):
            assert sensor.sense() == {}
        assert "fail_loud=False" in caplog.text

    @pytest.mark.parametrize("arm_name", ["jev", "fake"])
    def test_non_retryable_reuses_fresh_cache_when_fail_quiet(self, arm_name, caplog):
        arm = ARMS[arm_name]
        judge = _responder_judge(
            arm_name, JudgmentError("bad contract", backend=arm_name, retryable=False)
        )
        sensor = JudgmentSensor(
            judge,
            observe=lambda: arm.obs_a,
            questions=core_questions(),
            max_stale=30.0,
            min_interval=0.0,
            fail_loud=False,
        )
        # Prime the cache with a working judge first.
        working, _ = arm.make_judge()
        sensor._judge = working
        sensor.sense()
        sensor._judge = judge
        with caplog.at_level(logging.WARNING, logger="goapauto.models.judgment"):
            assert sensor.sense() == arm.expected_a
        assert "Reusing stale cache after judgment failure" in caplog.text
        stats = sensor.stats()
        assert (stats.calls, stats.errors, stats.stale_cache_hits) == (2, 1, 1)

    @pytest.mark.parametrize("arm_name", ["jev", "fake"])
    def test_expired_cache_not_reused(self, arm_name, caplog, monkeypatch):
        import goapauto.models.judgment as judgment_module

        arm = ARMS[arm_name]
        judge = _responder_judge(
            arm_name, JudgmentError("flaky", backend=arm_name, retryable=True)
        )
        ticks = [1000.0]
        monkeypatch.setattr(judgment_module.time, "monotonic", lambda: ticks[0])
        sensor = JudgmentSensor(
            judge,
            observe=lambda: arm.obs_a,
            questions=core_questions(),
            max_stale=30.0,
            min_interval=0.0,
        )
        # Prime the cache, then let it age past max_stale.
        working, _ = arm.make_judge()
        sensor._judge = working
        sensor.sense()
        sensor._judge = judge
        ticks[0] = 2000.0
        with caplog.at_level(logging.ERROR, logger="goapauto.models.judgment"):
            assert sensor.sense() == {}
        assert "no stale cache" in caplog.text
        stats = sensor.stats()
        assert (stats.calls, stats.errors, stats.stale_cache_hits) == (2, 1, 0)

    @pytest.mark.parametrize("arm_name", ["jev", "fake"])
    def test_non_judgment_error_propagates_unchanged(self, arm_name):
        boom = RuntimeError("boom")
        judge = _responder_judge(arm_name, boom)
        sensor = JudgmentSensor(judge, observe=lambda: {}, questions=core_questions())
        with pytest.raises(RuntimeError) as exc_info:
            sensor.sense()
        assert exc_info.value is boom

    @pytest.mark.parametrize("arm_name", ["jev", "fake"])
    def test_chained_error_names_the_cause(self, arm_name):
        records = []

        def responder(state, questions):
            try:
                raise ValueError("root cause")
            except ValueError as cause:
                raise JudgmentError(
                    "wrapped", backend=arm_name, retryable=False
                ) from cause

        from goapauto.testing import FakeTypeSafeClient

        if arm_name == "jev":
            from goapauto.models.jev import JevJudge

            judge = JevJudge(FakeTypeSafeClient(responder=responder))
        else:
            judge = FakeJudge(responder=responder)
        sensor = JudgmentSensor(
            judge,
            observe=lambda: {},
            questions=core_questions(),
            telemetry=records.append,
        )
        with pytest.raises(JudgmentError):
            sensor.sense()
        assert records[0].error == "ValueError"

    def test_rule_judge_negative_propagates_fail_loud(self):
        judge = RuleJudge({})
        sensor = JudgmentSensor(
            judge, observe=lambda: {"t": "nothing"}, questions=core_questions()
        )
        with pytest.raises(JudgmentError) as exc_info:
            sensor.sense()
        assert exc_info.value.backend == "rule"
        assert exc_info.value.retryable is False

    def test_rule_judge_negative_absorbed_fail_quiet(self):
        judge = RuleJudge({})
        sensor = JudgmentSensor(
            judge,
            observe=lambda: {"t": "nothing"},
            questions=core_questions(),
            fail_loud=False,
        )
        assert sensor.sense() == {}

    def test_telemetry_callback_exception_swallowed(self, caplog):
        def bad_telemetry(record):
            raise RuntimeError("telemetry boom")

        sensor, _ = make_sensor(ARMS["fake"], telemetry=bad_telemetry)
        with caplog.at_level(logging.ERROR, logger="goapauto.models.judgment"):
            sensor.sense()
        assert "telemetry" in caplog.text.lower()

    def test_telemetry_records_propagated_failure(self):
        records = []
        sensor = self._failing_sensor(records)
        with pytest.raises(JudgmentError):
            sensor.sense()
        (record,) = records
        assert record.error == "JudgmentError"
        assert record.backend == "fake"

    def _failing_sensor(self, records):
        def responder(state, questions):
            raise JudgmentError("nope", backend="fake", retryable=False)

        return JudgmentSensor(
            FakeJudge(responder=responder),
            observe=lambda: {},
            questions=core_questions(),
            telemetry=records.append,
        )


# ---------------------------------------------------------------------------
# JudgmentGoalStrategy, parametrized over backends.
# ---------------------------------------------------------------------------


def make_goals():
    return [
        Goal(name="Eat", target_state={"fed": True}, priority=2),
        Goal(name="Sleep", target_state={"rested": True}, priority=1),
    ]


class TestJudgmentGoalStrategy:
    def test_empty_goals_return_none_without_backend_call(self, arm):
        judge = STRATEGY_JUDGES[arm.name]("Sleep")
        calls = getattr(judge, "calls", None)
        strategy = JudgmentGoalStrategy(judge)
        assert strategy.select([], WorldState()) is None
        if calls is not None:
            assert calls == []

    def test_selects_backend_pick(self, arm):
        judge = STRATEGY_JUDGES[arm.name](arm.strategy_pick)
        strategy = JudgmentGoalStrategy(judge)
        goals = make_goals()
        expected = goals[["Eat", "Sleep"].index(arm.strategy_pick)]
        assert strategy.select(goals, WorldState()) is expected

    def test_label_falls_back_to_target_state(self):
        judge = FakeJudge(answers={"goal": ChoiceAnswer("fed")})
        strategy = JudgmentGoalStrategy(judge)
        goal = Goal(target_state={"fed": True})
        assert goal.name == "{'fed': True}"
        assert strategy.select([goal], WorldState()) is goal

    def test_duplicate_labels_rejected_before_backend_call(self, arm):
        judge = STRATEGY_JUDGES[arm.name](arm.strategy_pick)
        strategy = JudgmentGoalStrategy(judge)
        goals = [
            Goal(name="Eat", target_state={"fed": True}),
            Goal(name="Eat", target_state={"x": 1}),
        ]
        with pytest.raises(ValueError, match="unique goal names"):
            strategy.select(goals, WorldState())

    def test_retryable_error_falls_back_to_first_goal(self):
        def responder(state, questions):
            raise JudgmentError("flaky", backend="fake", retryable=True)

        strategy = JudgmentGoalStrategy(FakeJudge(responder=responder))
        goals = make_goals()
        assert strategy.select(goals, WorldState()) is goals[0]

    def test_unknown_label_falls_back_to_first_goal(self, caplog):
        strategy = JudgmentGoalStrategy(
            FakeJudge(answers={"goal": ChoiceAnswer("Nope")})
        )
        goals = make_goals()
        with caplog.at_level(logging.WARNING, logger="goapauto.models.judgment"):
            assert strategy.select(goals, WorldState()) is goals[0]
        assert "unknown goal" in caplog.text.lower()

    def test_non_retryable_propagates_when_fail_loud(self):
        records = []
        failure = JudgmentError("bad", backend="fake", retryable=False)

        def responder(state, questions):
            raise failure

        strategy = JudgmentGoalStrategy(
            FakeJudge(responder=responder), telemetry=records.append
        )
        with pytest.raises(JudgmentError) as exc_info:
            strategy.select(make_goals(), WorldState())
        assert exc_info.value is failure
        assert records[0].source == "strategy"
        assert records[0].error == "JudgmentError"
        assert records[0].backend == "fake"

    def test_non_retryable_absorbed_when_fail_quiet(self):
        def responder(state, questions):
            raise JudgmentError("bad", backend="fake", retryable=False)

        strategy = JudgmentGoalStrategy(FakeJudge(responder=responder), fail_loud=False)
        goals = make_goals()
        assert strategy.select(goals, WorldState()) is goals[0]

    def test_non_judgment_error_propagates_unchanged(self):
        boom = RuntimeError("boom")

        def responder(state, questions):
            raise boom

        strategy = JudgmentGoalStrategy(FakeJudge(responder=responder))
        with pytest.raises(RuntimeError) as exc_info:
            strategy.select(make_goals(), WorldState())
        assert exc_info.value is boom

    def test_state_payload_shape(self):
        seen = {}

        def responder(state, questions):
            seen["state"] = state
            seen["questions"] = questions
            return {"goal": ChoiceAnswer("Eat")}

        strategy = JudgmentGoalStrategy(FakeJudge(responder=responder))
        goals = make_goals()
        state = WorldState(fed=False)
        assert strategy.select(goals, state) is goals[0]
        assert seen["state"] == {
            "world_state": {"fed": False},
            "goals": [
                {"name": "Eat", "target_state": {"fed": True}},
                {"name": "Sleep", "target_state": {"rested": True}},
            ],
        }
        (question,) = seen["questions"].values()
        assert isinstance(question, ChoiceQuestion)
        assert question.instructions == "Which goal should the agent pursue next?"
        assert list(question.choices) == ["Eat", "Sleep"]
        assert list(question.descriptions.values()) == [
            '{"priority": 2, "target_state": {"fed": true}}',
            '{"priority": 1, "target_state": {"rested": true}}',
        ]

    def test_strategy_telemetry_copies_usage_fields(self):
        response = JudgmentResponse(
            answers={"goal": ChoiceAnswer("Eat")},
            backend="fake",
            usage=TokenUsage(input_tokens=5, output_tokens=6),
        )

        class UsageJudge:
            backend = "fake"

            def judge(self, state, questions):
                return response

            def close(self):
                pass

        records = []
        strategy = JudgmentGoalStrategy(UsageJudge(), telemetry=records.append)
        goals = make_goals()
        assert strategy.select(goals, WorldState()) is goals[0]
        assert (records[0].input_tokens, records[0].output_tokens) == (5, 6)

    def test_custom_instructions_used(self):
        seen = {}

        def responder(state, questions):
            seen["questions"] = questions
            return {"goal": ChoiceAnswer("Eat")}

        strategy = JudgmentGoalStrategy(
            FakeJudge(responder=responder), instructions="Pick one."
        )
        goals = make_goals()
        assert strategy.select(goals, WorldState()) is goals[0]
        (question,) = seen["questions"].values()
        assert question.instructions == "Pick one."

    def test_strategy_close_and_backend_name(self, arm):
        judge = STRATEGY_JUDGES[arm.name](arm.strategy_pick)
        strategy = JudgmentGoalStrategy(judge)
        assert strategy.backend_name() == arm.name
        strategy.close()

    def test_strategy_stats(self):
        strategy = JudgmentGoalStrategy(
            FakeJudge(answers={"goal": ChoiceAnswer("Eat")})
        )
        strategy.select(make_goals(), WorldState())
        stats = strategy.stats()
        assert (stats.calls, stats.errors) == (1, 0)

    def test_none_judge_rejected(self):
        with pytest.raises(TypeError, match="needs a judge"):
            JudgmentGoalStrategy(None)

    def test_missing_goal_answer_is_fatal(self):
        judge = FakeJudge(responder=lambda state, questions: {})
        strategy = JudgmentGoalStrategy(judge)
        with pytest.raises(JudgmentError, match="Missing answer"):
            strategy.select(make_goals(), WorldState())

    def test_wrong_goal_answer_type_is_fatal(self):
        judge = FakeJudge(responder=lambda state, questions: {"goal": FloatAnswer(0.5)})
        strategy = JudgmentGoalStrategy(judge)
        with pytest.raises(JudgmentError, match="Wrong answer type"):
            strategy.select(make_goals(), WorldState())

    def test_telemetry_callback_exception_swallowed(self, caplog):
        def bad_telemetry(record):
            raise RuntimeError("telemetry boom")

        strategy = JudgmentGoalStrategy(
            FakeJudge(answers={"goal": ChoiceAnswer("Eat")}),
            telemetry=bad_telemetry,
        )
        goals = make_goals()
        with caplog.at_level(logging.ERROR, logger="goapauto.models.judgment"):
            assert strategy.select(goals, WorldState()) is goals[0]
        assert "telemetry" in caplog.text.lower()


# ---------------------------------------------------------------------------
# Public re-exports.
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_core_symbols_importable_from_package_root(self):
        import goapauto

        for name in (
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
        ):
            assert name in goapauto.__all__, name
            assert hasattr(goapauto, name), name

    def test_jev_judge_lazy_export(self):
        import goapauto
        from goapauto.models.jev import JevJudge

        assert "JevJudge" in goapauto.__all__
        assert goapauto.JevJudge is JevJudge
