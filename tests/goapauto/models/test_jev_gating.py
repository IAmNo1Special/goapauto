"""Tests for Wave 2 differentiator 04: confidence gating (design 04).

Gating is opt-in and fully off by default: with no threshold configured, no
confidence attribute is read, no threshold is validated, no gate decision is
emitted, and behavior is byte-identical to 0.5.0 (those suites pass
unmodified).
"""

from __future__ import annotations

import dataclasses
import logging
import math

import pytest
from typesafe_sdk import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    SystemOneResponse,
    Usage,
)

import goapauto
import goapauto.models.jev as jev_module
from goapauto import GateDecision
from goapauto.models.goal import Goal
from goapauto.models.jev import (
    JevGoalStrategy,
    JevSensor,
    TypeSafeError,
    _answer_confidence,
    _CorruptConfidenceError,
)
from goapauto.models.worldstate import WorldState

QUESTIONS = {
    "danger": Noul(instructions="Is there immediate danger?"),
    "threat": Choice(
        instructions="What is the threat doing?",
        criteria={"hunting": "actively hunting", "resting": "at rest"},
    ),
    "hunger": Score(
        instructions="How hungry is the agent?",
        criteria=["not hungry", "hungry", "starving"],
    ),
}
MAPPING = {"danger": "danger", "threat": "threat", "hunger": "hunger"}


def _answers(
    *,
    threat_confidence=0.9,
    hunger_confidence=0.95,
    threat_choice="hunting",
    danger_noul=0.87,
):
    return {
        "danger": NoulAnswer(noul=danger_noul),
        "threat": ChoiceAnswer(
            choice=threat_choice,
            confidence=threat_confidence,
            probabilities={threat_choice: 1.0},
        ),
        "hunger": ScoreAnswer(
            score=0.62,
            confidence=hunger_confidence,
            legend={0: "not hungry", 1: "hungry", 2: "starving"},
            probabilities={0: 0.1, 1: 0.28, 2: 0.62},
        ),
    }


def _response(**kwargs):
    return SystemOneResponse.model_construct(
        answers=_answers(**kwargs),
        usage=Usage(input_tokens=10, output_tokens=5),
    )


class _ScriptedClient:
    """TypeSafeClient stub replaying scripted responses/exceptions in order."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def system_one(self, observation, questions):
        self.calls.append((observation, questions))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def _sensor(client, **kwargs):
    return JevSensor(
        observe=lambda: {"x": 1},
        questions=QUESTIONS,
        mapping=MAPPING,
        client=client,
        **kwargs,
    )


def _goals():
    return [
        Goal(name="Flee", target_state={"threat": "gone"}, priority=2),
        Goal(name="Eat", target_state={"hunger": 0.0}, priority=1),
        Goal(name="Sleep", target_state={"energy": 1.0}, priority=3),
    ]


def _strategy_response(pick="Sleep", confidence=0.9):
    return SystemOneResponse.model_construct(
        answers={
            "goal": ChoiceAnswer(
                choice=pick, confidence=confidence, probabilities={pick: 1.0}
            )
        },
        usage=Usage(input_tokens=10, output_tokens=5),
    )


class TestGateDecision:
    def test_valid_combinations_construct(self):
        GateDecision("threat", "threat", 0.42, 0.8, "blocked", "low_confidence", "drop")
        GateDecision("threat", "threat", 0.9, 0.8, "proceed", "passed", None)
        GateDecision(
            "danger", "danger", None, 0.8, "proceed", "absent_confidence", None
        )
        GateDecision("goal", None, 0.2, 0.8, "blocked", "low_confidence", "priority")

    def test_frozen(self):
        decision = GateDecision("threat", "threat", 0.9, 0.8, "proceed", "passed", None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.decision = "blocked"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "decision,reason,fallback,confidence,threshold",
        [
            ("blocked", "passed", "drop", 0.4, 0.8),
            ("proceed", "low_confidence", None, 0.4, 0.8),
            ("blocked", "low_confidence", None, 0.4, 0.8),
            ("proceed", "passed", "drop", 0.9, 0.8),
            ("proceed", "absent_confidence", None, 0.5, 0.8),
            ("proceed", "passed", None, None, 0.8),
            ("proceed", "passed", None, 0.9, 1.5),
            ("proceed", "passed", None, 1.5, 0.8),
        ],
    )
    def test_invariants_rejected(
        self, decision, reason, fallback, confidence, threshold
    ):
        with pytest.raises(ValueError):
            GateDecision("q", "k", confidence, threshold, decision, reason, fallback)

    def test_corrupt_error_is_a_public_typesafe_error(self):
        assert issubclass(_CorruptConfidenceError, TypeSafeError)


class TestThresholdValidation:
    @pytest.mark.parametrize("bad", [-0.1, 1.1, math.nan, "high"])
    def test_sensor_rejects_bad_global_threshold(self, bad):
        with pytest.raises(ValueError, match="confidence_threshold"):
            _sensor(_ScriptedClient([]), confidence_threshold=bad)

    @pytest.mark.parametrize("bad", [-0.1, 1.1, math.nan])
    def test_strategy_rejects_bad_threshold(self, bad):
        with pytest.raises(ValueError, match="confidence_threshold"):
            JevGoalStrategy(client=_ScriptedClient([]), confidence_threshold=bad)

    def test_sensor_rejects_bad_per_question_value(self):
        with pytest.raises(ValueError, match="confidence_thresholds"):
            _sensor(_ScriptedClient([]), confidence_thresholds={"threat": 1.5})

    def test_sensor_rejects_none_per_question_value(self):
        with pytest.raises(ValueError, match="got None"):
            _sensor(_ScriptedClient([]), confidence_thresholds={"threat": None})

    def test_sensor_rejects_unknown_per_question_key(self):
        with pytest.raises(TypeSafeError, match="question name"):
            _sensor(_ScriptedClient([]), confidence_thresholds={"nope": 0.5})

    def test_sensor_rejects_threshold_for_unmapped_question(self):
        with pytest.raises(TypeSafeError, match="question name"):
            JevSensor(
                observe=lambda: {},
                questions=QUESTIONS,
                mapping={"danger": "danger", "hunger": "hunger"},
                client=_ScriptedClient([]),
                confidence_thresholds={"threat": 0.5},
            )

    def test_strategy_rejects_bad_fallback_policy(self):
        with pytest.raises(ValueError, match="fallback_policy"):
            JevGoalStrategy(client=_ScriptedClient([]), fallback_policy="random")

    def test_boundary_thresholds_accepted(self):
        _sensor(_ScriptedClient([]), confidence_threshold=0.0)
        _sensor(_ScriptedClient([]), confidence_threshold=1.0)
        JevGoalStrategy(client=_ScriptedClient([]), confidence_threshold=0.0)
        JevGoalStrategy(client=_ScriptedClient([]), confidence_threshold=1.0)


class TestAnswerConfidence:
    def test_noul_never_reads_confidence(self):
        assert _answer_confidence(Noul(instructions="D?"), object()) is None

    def test_choice_confidence_returned(self):
        question = Choice(instructions="T?", criteria={"a": "A"})
        answer = ChoiceAnswer(choice="a", confidence=0.7, probabilities={"a": 1.0})
        assert _answer_confidence(question, answer) == 0.7

    def test_none_confidence_means_absent(self):
        question = Choice(instructions="T?", criteria={"a": "A"})
        answer = ChoiceAnswer.model_construct(
            choice="a", confidence=None, probabilities={"a": 1.0}
        )
        assert _answer_confidence(question, answer) is None

    def test_score_confidence_returned(self):
        answer = ScoreAnswer(
            score=0.5,
            confidence=0.3,
            legend={0: "low", 1: "high"},
            probabilities={0: 0.5, 1: 0.5},
        )
        assert _answer_confidence(QUESTIONS["hunger"], answer) == 0.3

    @pytest.mark.parametrize("bad", [math.nan, -0.1, 1.5, "high"])
    def test_corrupt_confidence_raises(self, bad):
        question = Choice(instructions="T?", criteria={"a": "A"})
        answer = ChoiceAnswer.model_construct(
            choice="a", confidence=bad, probabilities={"a": 1.0}
        )
        with pytest.raises(TypeSafeError, match="not a float in"):
            _answer_confidence(question, answer)


class TestGatingOffByteIdentical:
    def test_nan_confidence_passes_silently_when_off(self):
        client = _ScriptedClient([_response(threat_confidence=math.nan)])
        sensor = _sensor(client)
        assert sensor.sense() == {"danger": 0.87, "threat": "hunting", "hunger": 0.62}
        assert sensor.stats().errors == 0

    def test_confidence_never_derived_when_off(self, mocker):
        spy = mocker.patch(
            "goapauto.models.jev._answer_confidence",
            side_effect=AssertionError("must not be called"),
        )
        sensor = _sensor(_ScriptedClient([_response()]))
        sensor.sense()
        spy.assert_not_called()

    def test_off_telemetry_has_no_gate_fields(self):
        records = []
        sensor = _sensor(_ScriptedClient([_response()]), telemetry=records.append)
        sensor.sense()
        (record,) = records
        assert record.gate_decisions == ()
        assert record.gated_questions == ()
        assert record.unknown_confidence_questions == ()
        assert record.gating_enabled is False
        assert record.fully_gated is False
        assert record.gated is False
        assert record.confidence_absent is False
        assert record.pick_label is None
        assert record.label_matched is True
        assert record.fallback_reason is None
        assert sensor.stats().gated == 0

    def test_off_diagnostics(self):
        diagnostics = _sensor(_ScriptedClient([])).diagnostics()
        # 04 gating keys; 02 caching keys merge into the same mapping.
        assert {
            "gating_enabled": False,
            "effective_thresholds": {},
            "gated_questions_last_call": [],
            "fully_gated_last_call": False,
            "gated_total": 0,
        }.items() <= diagnostics.items()

    def test_strategy_nan_pick_honored_when_off(self):
        client = _ScriptedClient(
            [_strategy_response(pick="Sleep", confidence=math.nan)]
        )
        strategy = JevGoalStrategy(client=client)
        goals = _goals()
        assert strategy.select(goals, WorldState(hunger=0.9)) is goals[2]
        assert strategy.stats().gated == 0


class TestSensorGating:
    def test_below_threshold_dropped_others_pass(self):
        records = []
        client = _ScriptedClient([_response(threat_confidence=0.2)])
        sensor = _sensor(client, confidence_threshold=0.8, telemetry=records.append)
        assert sensor.sense() == {"danger": 0.87, "hunger": 0.62}
        (record,) = records
        assert [d.question for d in record.gate_decisions] == [
            "danger",
            "threat",
            "hunger",
        ]
        blocked = [d for d in record.gate_decisions if d.decision == "blocked"]
        assert len(blocked) == 1
        (decision,) = blocked
        assert (
            decision.question,
            decision.key,
            decision.confidence,
            decision.threshold,
            decision.reason,
            decision.fallback,
        ) == ("threat", "threat", 0.2, 0.8, "low_confidence", "drop")
        assert record.gated_questions == ("threat",)
        assert record.unknown_confidence_questions == ("danger",)
        assert record.gating_enabled is True
        assert record.fully_gated is False
        assert sensor.stats().gated == 1

    def test_boundary_confidence_equal_to_threshold_passes(self):
        sensor = _sensor(
            _ScriptedClient([_response(threat_confidence=0.8)]),
            confidence_threshold=0.8,
        )
        assert sensor.sense()["threat"] == "hunting"
        assert sensor.stats().gated == 0

    def test_per_question_threshold_wins_over_global(self):
        sensor = _sensor(
            _ScriptedClient([_response(threat_confidence=0.7, hunger_confidence=0.7)]),
            confidence_threshold=0.9,
            confidence_thresholds={"threat": 0.5},
        )
        result = sensor.sense()
        assert result["threat"] == "hunting"
        assert "hunger" not in result

    def test_per_question_only_threshold(self):
        records = []
        sensor = _sensor(
            _ScriptedClient([_response(threat_confidence=0.2, hunger_confidence=0.2)]),
            confidence_thresholds={"threat": 0.8},
            telemetry=records.append,
        )
        result = sensor.sense()
        assert "threat" not in result
        assert result["hunger"] == 0.62
        (record,) = records
        assert [d.question for d in record.gate_decisions] == ["threat"]
        assert sensor.diagnostics()["effective_thresholds"] == {"threat": 0.8}

    def test_zero_threshold_gates_nothing_but_enables(self):
        records = []
        sensor = _sensor(
            _ScriptedClient([_response(threat_confidence=0.0)]),
            confidence_threshold=0.0,
            telemetry=records.append,
        )
        assert sensor.sense() == {"danger": 0.87, "threat": "hunting", "hunger": 0.62}
        (record,) = records
        assert record.gating_enabled is True
        assert len(record.gate_decisions) == 3
        assert all(d.decision == "proceed" for d in record.gate_decisions)
        assert sensor.stats().gated == 0

    def test_fully_gated_returns_empty_and_advances_success(self, caplog):
        questions = {"threat": QUESTIONS["threat"], "hunger": QUESTIONS["hunger"]}
        records = []
        client = _ScriptedClient(
            [_response(threat_confidence=0.1, hunger_confidence=0.1)]
        )
        sensor = JevSensor(
            observe=lambda: {"x": 1},
            questions=questions,
            mapping={"threat": "threat", "hunger": "hunger"},
            client=client,
            confidence_threshold=0.8,
            telemetry=records.append,
        )
        assert sensor.sense() == {}
        (record,) = records
        assert record.fully_gated is True
        assert record.gated_questions == ("threat", "hunger")
        assert sensor.stats().gated == 2
        assert sensor.diagnostics()["fully_gated_last_call"] is True
        # Success time advanced: the next failing call takes the stale-cache
        # path (warning), not the dead-cache path (error).
        client._script.append(TypeSafeError("boom"))
        with caplog.at_level(logging.WARNING, logger="goapauto.models.jev"):
            assert sensor.sense() == {}
        assert "reusing last judgments" in caplog.text

    def test_gated_key_keeps_cached_value(self):
        observations = [{"x": 1}, {"x": 2}, {"x": 3}]
        client = _ScriptedClient(
            [
                _response(threat_confidence=0.9),
                _response(threat_confidence=0.1, threat_choice="resting"),
                TypeSafeError("provider down"),
            ]
        )
        sensor = JevSensor(
            observe=lambda: observations.pop(0),
            questions=QUESTIONS,
            mapping=MAPPING,
            client=client,
            confidence_threshold=0.8,
        )
        assert sensor.sense() == {"danger": 0.87, "threat": "hunting", "hunger": 0.62}
        assert sensor.sense() == {"danger": 0.87, "hunger": 0.62}
        # 02 refactor: per-key cache (value, as_of); gated key retains value.
        assert sensor._cache._entries["threat"][0] == "hunting"
        assert sensor.stats().gated == 1
        assert sensor.sense() == {"danger": 0.87, "threat": "hunting", "hunger": 0.62}

    def test_corrupt_confidence_raises_and_records(self):
        records = []
        sensor = _sensor(
            _ScriptedClient([_response(threat_confidence=math.nan)]),
            confidence_threshold=0.8,
            telemetry=records.append,
        )
        with pytest.raises(TypeSafeError, match="not a float in"):
            sensor.sense()
        (record,) = records
        assert record.error == "_CorruptConfidenceError"
        assert record.gate_decisions == ()
        assert record.gating_enabled is True
        assert sensor.stats().errors == 1
        assert sensor.stats().gated == 0

    def test_corrupt_confidence_direct_extract_raises(self):
        sensor = _sensor(_ScriptedClient([]), confidence_threshold=0.8)
        with pytest.raises(TypeSafeError):
            sensor._extract(_response(threat_confidence=2.0))

    def test_aggregated_log_line(self, caplog):
        sensor = _sensor(
            _ScriptedClient([_response(threat_confidence=0.2)]),
            confidence_threshold=0.8,
        )
        with caplog.at_level(logging.INFO, logger="goapauto.models.jev"):
            sensor.sense()
        assert "blocked=['threat' (confidence=0.2 < threshold=0.8)]" in caplog.text
        assert "absent_confidence=['danger']" in caplog.text

    def test_diagnostics(self):
        observations = [{"x": 1}, {"x": 2}]
        client = _ScriptedClient(
            [
                _response(threat_confidence=0.2),
                _response(threat_confidence=0.9),
            ]
        )
        sensor = JevSensor(
            observe=lambda: observations.pop(0),
            questions=QUESTIONS,
            mapping=MAPPING,
            client=client,
            confidence_threshold=0.8,
            confidence_thresholds={"hunger": 0.5},
        )
        sensor.sense()
        diagnostics = sensor.diagnostics()
        # 04 gating keys; 02 caching keys merge into the same mapping.
        assert {
            "gating_enabled": True,
            "effective_thresholds": {"danger": 0.8, "threat": 0.8, "hunger": 0.5},
            "gated_questions_last_call": ["threat"],
            "fully_gated_last_call": False,
            "gated_total": 1,
        }.items() <= diagnostics.items()
        sensor.sense()
        assert sensor.diagnostics()["gated_questions_last_call"] == []
        assert sensor.diagnostics()["gated_total"] == 1

    def test_error_path_resets_last_gate_info(self):
        observations = [{"x": 1}, {"x": 2}]
        client = _ScriptedClient(
            [
                _response(threat_confidence=0.2),
                TypeSafeError("down"),
            ]
        )
        sensor = JevSensor(
            observe=lambda: observations.pop(0),
            questions=QUESTIONS,
            mapping=MAPPING,
            client=client,
            confidence_threshold=0.8,
        )
        sensor.sense()
        assert sensor.diagnostics()["gated_questions_last_call"] == ["threat"]
        sensor.sense()
        assert sensor.diagnostics()["gated_questions_last_call"] == []
        assert sensor.diagnostics()["gating_enabled"] is True


class TestStrategyGating:
    def test_low_confidence_pick_falls_back_first(self):
        records = []
        client = _ScriptedClient([_strategy_response(pick="Sleep", confidence=0.2)])
        strategy = JevGoalStrategy(
            client=client, confidence_threshold=0.8, telemetry=records.append
        )
        goals = _goals()
        assert strategy.select(goals, WorldState(hunger=0.9)) is goals[0]
        (record,) = records
        assert record.gated is True
        assert record.fallback_reason == "low_confidence"
        assert record.pick_label == "Sleep"
        assert record.label_matched is True
        assert record.gating_enabled is True
        assert len(record.gate_decisions) == 1
        (decision,) = record.gate_decisions
        assert (
            decision.question,
            decision.key,
            decision.confidence,
            decision.threshold,
            decision.decision,
            decision.reason,
            decision.fallback,
        ) == ("goal", None, 0.2, 0.8, "blocked", "low_confidence", "first")
        assert strategy.stats().gated == 1

    def test_boundary_confidence_equal_to_threshold_honored(self):
        client = _ScriptedClient([_strategy_response(pick="Sleep", confidence=0.8)])
        strategy = JevGoalStrategy(client=client, confidence_threshold=0.8)
        goals = _goals()
        assert strategy.select(goals, WorldState()) is goals[2]
        assert strategy.stats().gated == 0

    def test_priority_fallback_policy(self):
        client = _ScriptedClient([_strategy_response(pick="Sleep", confidence=0.1)])
        strategy = JevGoalStrategy(
            client=client, confidence_threshold=0.8, fallback_policy="priority"
        )
        goals = _goals()
        assert strategy.select(goals, WorldState()) is goals[1]

    def test_priority_fallback_tie_keeps_list_order(self):
        goals = [
            Goal(name="A", target_state={"x": 1}, priority=1),
            Goal(name="B", target_state={"y": 1}, priority=1),
        ]
        client = _ScriptedClient([_strategy_response(pick="B", confidence=0.1)])
        strategy = JevGoalStrategy(
            client=client, confidence_threshold=0.8, fallback_policy="priority"
        )
        assert strategy.select(goals, WorldState()) is goals[0]

    def test_priority_fallback_on_provider_error(self):
        client = _ScriptedClient([TypeSafeError("down")])
        strategy = JevGoalStrategy(
            client=client, confidence_threshold=0.8, fallback_policy="priority"
        )
        goals = _goals()
        assert strategy.select(goals, WorldState()) is goals[1]

    def test_low_confidence_unknown_label(self):
        records = []
        client = _ScriptedClient([_strategy_response(pick="Ghost", confidence=0.1)])
        strategy = JevGoalStrategy(
            client=client, confidence_threshold=0.8, telemetry=records.append
        )
        goals = _goals()
        assert strategy.select(goals, WorldState()) is goals[0]
        (record,) = records
        assert record.fallback_reason == "low_confidence"
        assert record.label_matched is False
        assert record.pick_label == "Ghost"

    def test_unknown_label_high_confidence(self):
        records = []
        client = _ScriptedClient([_strategy_response(pick="Ghost", confidence=0.9)])
        strategy = JevGoalStrategy(
            client=client, confidence_threshold=0.8, telemetry=records.append
        )
        goals = _goals()
        assert strategy.select(goals, WorldState()) is goals[0]
        (record,) = records
        assert record.fallback_reason == "unknown_label"
        assert record.label_matched is False
        assert record.gated is False
        assert record.gate_decisions[0].decision == "proceed"

    def test_error_path_telemetry(self):
        records = []
        client = _ScriptedClient([TypeSafeError("down")])
        strategy = JevGoalStrategy(
            client=client, confidence_threshold=0.8, telemetry=records.append
        )
        goals = _goals()
        assert strategy.select(goals, WorldState()) is goals[0]
        (record,) = records
        assert record.error == "TypeSafeError"
        assert record.fallback_reason == "error"
        assert record.pick_label is None
        assert record.label_matched is True
        assert record.gate_decisions == ()
        assert record.gated is False
        assert strategy.stats().gated == 0

    def test_absent_confidence_proceeds_flagged(self):
        records = []
        response = SystemOneResponse.model_construct(
            answers={
                "goal": ChoiceAnswer.model_construct(
                    choice="Sleep", confidence=None, probabilities={"Sleep": 1.0}
                )
            },
            usage=Usage(input_tokens=1, output_tokens=1),
        )
        strategy = JevGoalStrategy(
            client=_ScriptedClient([response]),
            confidence_threshold=0.8,
            telemetry=records.append,
        )
        goals = _goals()
        assert strategy.select(goals, WorldState()) is goals[2]
        (record,) = records
        assert record.confidence_absent is True
        assert record.gated is False
        (decision,) = record.gate_decisions
        assert (decision.decision, decision.reason, decision.confidence) == (
            "proceed",
            "absent_confidence",
            None,
        )

    def test_corrupt_confidence_raises_after_recording(self):
        records = []
        client = _ScriptedClient(
            [_strategy_response(pick="Sleep", confidence=math.nan)]
        )
        strategy = JevGoalStrategy(
            client=client, confidence_threshold=0.8, telemetry=records.append
        )
        goals = _goals()
        with pytest.raises(TypeSafeError, match="not a float in"):
            strategy.select(goals, WorldState())
        (record,) = records
        assert record.error == "_CorruptConfidenceError"
        assert record.fallback_reason == "error"
        assert record.gate_decisions == ()
        assert strategy.stats().errors == 1
        assert strategy.stats().gated == 0

    def test_strategy_block_log_line(self, caplog):
        client = _ScriptedClient([_strategy_response(pick="Sleep", confidence=0.2)])
        strategy = JevGoalStrategy(
            client=client, confidence_threshold=0.8, fallback_policy="priority"
        )
        strategy.select(_goals(), WorldState())
        assert (
            "blocked low-confidence pick 'Sleep' "
            "(confidence=0.200 < threshold=0.800)" in caplog.text
        )
        assert "falling back via 'priority' policy" in caplog.text

    def test_gating_off_records_pick_facts(self):
        records = []
        client = _ScriptedClient([_strategy_response(pick="Eat", confidence=0.9)])
        strategy = JevGoalStrategy(client=client, telemetry=records.append)
        goals = _goals()
        assert strategy.select(goals, WorldState()) is goals[1]
        (record,) = records
        assert record.gate_decisions == ()
        assert record.gating_enabled is False
        assert record.pick_label == "Eat"
        assert record.label_matched is True
        assert record.fallback_reason is None


class TestExports:
    def test_gate_decision_exported(self):
        assert goapauto.GateDecision is jev_module.GateDecision
        assert "GateDecision" in goapauto.__all__
        assert "GateDecision" in jev_module.__all__
