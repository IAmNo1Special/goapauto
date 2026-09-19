import pytest
from typesafe_sdk import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    SystemOneResponse,
    TypeSafeClient,
    TypeSafeError,
    Usage,
)

from goapauto.models.goal import Goal
from goapauto.models.goal_arbitrator import GoalArbitrator
from goapauto.models.jev import (
    JevGoalStrategy,
    JevSensor,
    _answers_of,
    _json_safe,
    _value_of,
)
from goapauto.models.sensors import SensorManager
from goapauto.models.worldstate import WorldState


def usage():
    return Usage(input_tokens=10, output_tokens=2)


def sdk_response():
    return SystemOneResponse(
        model="jev-latest",
        usage=usage(),
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


def dict_response():
    return {
        "model": "jev-latest",
        "answers": {
            "danger": {"type": "noul", "noul": 0.87},
            "threat": {"type": "choice", "choice": "hunting"},
            "hunger": {"type": "score", "score": 2.0},
        },
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


def questions():
    return {
        "danger": {"type": "noul", "instructions": "Is the agent in danger?"},
        "threat": {
            "type": "choice",
            "instructions": "What is the nearest creature doing?",
            "criteria": {"hunting": None, "sleeping": None},
        },
        "hunger": {
            "type": "score",
            "instructions": "How urgent is food?",
            "criteria": ["not hungry", "hungry", "starving"],
        },
    }


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


class TestHelpers:
    def test_answers_of_sdk(self):
        assert _answers_of(sdk_response())["danger"].noul == 0.87

    def test_answers_of_dict(self):
        assert _answers_of(dict_response())["danger"]["noul"] == 0.87

    def test_answers_of_dict_missing(self):
        with pytest.raises(TypeSafeError, match="Missing 'answers'"):
            _answers_of({"model": "x"})

    def test_answers_of_dict_invalid(self):
        with pytest.raises(TypeSafeError, match="Invalid 'answers'"):
            _answers_of({"answers": ["not", "a", "dict"]})

    def test_value_of_sdk_noul(self):
        assert _value_of(NoulAnswer(noul=0.5)) == 0.5

    def test_value_of_sdk_choice(self):
        answer = ChoiceAnswer(choice="a", confidence=1.0, probabilities={"a": 1.0})
        assert _value_of(answer) == "a"

    def test_value_of_sdk_score(self):
        answer = ScoreAnswer(
            score=1.0,
            confidence=1.0,
            legend={0: "low", 1: "high"},
            probabilities={0: 0.2, 1: 0.8},
        )
        assert _value_of(answer) == 1.0

    def test_value_of_dict_noul(self):
        assert _value_of({"type": "noul", "noul": 0.3}) == 0.3

    def test_value_of_dict_choice(self):
        assert _value_of({"type": "choice", "choice": "x"}) == "x"

    def test_value_of_dict_score(self):
        assert _value_of({"type": "score", "score": 1.0}) == 1.0

    def test_value_of_dict_unknown(self):
        with pytest.raises(TypeSafeError, match="Unknown answer type"):
            _value_of({"type": "weird"})

    def test_value_of_dict_missing_type(self):
        with pytest.raises(TypeSafeError, match="Unknown answer type"):
            _value_of({"noul": 0.5})

    def test_value_of_other_unknown(self):
        with pytest.raises(TypeSafeError, match="Unknown answer type"):
            _value_of(object())

    def test_json_safe_primitives(self):
        assert _json_safe(None) is None
        assert _json_safe("a") == "a"
        assert _json_safe(1) == 1
        assert _json_safe(1.5) == 1.5
        assert _json_safe(True) is True

    def test_json_safe_callable(self):
        def yes(v):
            return v

        assert _json_safe(yes) == "yes"

    def test_json_safe_callable_no_name(self):
        assert _json_safe(callable) is not None

    def test_json_safe_dict(self):
        def pred(v):
            return True

        assert _json_safe({"a": pred, 1: [pred]}) == {"a": "pred", "1": ["pred"]}

    def test_json_safe_list_tuple(self):
        assert _json_safe([1, "a"]) == [1, "a"]
        assert _json_safe((1, "a")) == [1, "a"]

    def test_json_safe_other(self):
        assert _json_safe(object()) is not None

    def test_sdk_reexports(self):
        from goapauto.models import jev

        assert jev.TypeSafeClient is TypeSafeClient
        assert jev.TypeSafeError is TypeSafeError
        assert "TypeSafeClient" in jev.__all__
        assert "JevSensor" in jev.__all__


class TestJevSensor:
    def test_sense_maps_dict_answers(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = dict_response()
        observation = {"enemies_nearby": 3, "health": 20}
        sensor = JevSensor(
            observe=lambda: observation, questions=questions(), client=api
        )

        assert sensor.sense() == {"danger": 0.87, "threat": "hunting", "hunger": 2.0}
        api.system_one.assert_called_once_with(observation, questions())
        sensor.sense()
        assert api.system_one.call_count == 2

    def test_sense_maps_sdk_answers(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = sdk_response()
        observation = {"enemies_nearby": 3, "health": 20}
        sensor = JevSensor(
            observe=lambda: observation, questions=sdk_questions(), client=api
        )

        assert sensor.sense() == {"danger": 0.87, "threat": "hunting", "hunger": 2.0}
        api.system_one.assert_called_once_with(observation, sensor._questions)

    def test_sense_sdk_individual_types(self, mocker):
        for name, expected in [
            ("danger", 0.87),
            ("threat", "hunting"),
            ("hunger", 2.0),
        ]:
            api = mocker.Mock()
            single = {k: v for k, v in sdk_response().answers.items() if k == name}
            api.system_one.return_value = SystemOneResponse(
                model="jev-latest", usage=usage(), answers=single
            )
            sensor = JevSensor(
                observe=lambda: {},
                questions={name: questions()[name]},
                client=api,
            )
            assert sensor.sense() == {name: expected}

    def test_custom_mapping(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = dict_response()
        sensor = JevSensor(
            observe=lambda: {},
            questions=questions(),
            mapping={"danger": "is_dangerous"},
            client=api,
        )
        assert sensor.sense() == {"is_dangerous": 0.87}

    def test_custom_mapping_sdk(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = sdk_response()
        sensor = JevSensor(
            observe=lambda: {},
            questions=sdk_questions(),
            mapping={"threat": "activity"},
            client=api,
        )
        assert sensor.sense() == {"activity": "hunting"}

    def test_empty_questions_rejected(self):
        with pytest.raises(TypeSafeError, match="at least one question"):
            JevSensor(observe=lambda: {}, questions={})

    def test_mapping_unknown_question_rejected(self, mocker):
        with pytest.raises(TypeSafeError, match="unknown questions"):
            JevSensor(
                observe=lambda: {},
                questions=questions(),
                mapping={"nope": "x"},
                client=mocker.Mock(),
            )

    def test_unknown_answer_type(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.return_value = {"answers": {"danger": {"type": "weird"}}}
        sensor = JevSensor(observe=lambda: {}, questions=questions(), client=api)
        assert sensor.sense() == {}
        assert "Unknown answer type" in caplog.text

    def test_unknown_answer_object(self, mocker, caplog):
        class FakeResponse:
            answers = {"danger": object()}

        api = mocker.Mock()
        api.system_one.return_value = FakeResponse()
        sensor = JevSensor(observe=lambda: {}, questions=questions(), client=api)
        assert sensor.sense() == {}
        assert "Unknown answer type" in caplog.text

    def test_missing_answer(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.return_value = {"answers": {}}
        sensor = JevSensor(observe=lambda: {}, questions=questions(), client=api)
        assert sensor.sense() == {}
        assert "Missing answer" in caplog.text

    def test_missing_answers_key(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.return_value = {"model": "jev-latest"}
        sensor = JevSensor(observe=lambda: {}, questions=questions(), client=api)
        assert sensor.sense() == {}
        assert "reusing last judgments" in caplog.text

    def test_invalid_answers_shape(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.return_value = {"answers": []}
        sensor = JevSensor(observe=lambda: {}, questions=questions(), client=api)
        assert sensor.sense() == {}
        assert "reusing last judgments" in caplog.text

    def test_api_error_first_call_returns_empty(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.side_effect = TypeSafeError("down")
        sensor = JevSensor(observe=lambda: {}, questions=questions(), client=api)
        assert sensor.sense() == {}
        assert "reusing last judgments" in caplog.text

    def test_api_error_reuses_cached(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.side_effect = [
            dict_response(),
            TypeSafeError("down"),
        ]
        sensor = JevSensor(observe=lambda: {"x": 1}, questions=questions(), client=api)
        assert sensor.sense()["danger"] == 0.87
        assert sensor.sense()["danger"] == 0.87
        assert "reusing last judgments" in caplog.text

    def test_min_interval_skips_call(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = dict_response()
        sensor = JevSensor(
            observe=lambda: {"x": 1},
            questions=questions(),
            client=api,
            min_interval=60.0,
        )
        sensor.sense()
        sensor.sense()
        assert api.system_one.call_count == 1

    def test_resense_on_change(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = dict_response()
        observation = [{"x": 1}, {"x": 2}]
        sensor = JevSensor(
            observe=lambda: observation.pop(0),
            questions=questions(),
            client=api,
            min_interval=60.0,
        )
        sensor.sense()
        sensor.sense()
        assert api.system_one.call_count == 2

    def test_no_resense_on_change_when_disabled(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = dict_response()
        observation = [{"x": 1}, {"x": 2}]
        sensor = JevSensor(
            observe=lambda: observation.pop(0),
            questions=questions(),
            client=api,
            min_interval=60.0,
            resense_on_change=False,
        )
        sensor.sense()
        sensor.sense()
        assert api.system_one.call_count == 1

    def test_interval_elapsed_resenses(self, mocker):
        now = [1000.0]
        mocker.patch("time.monotonic", side_effect=lambda: now[0])
        api = mocker.Mock()
        api.system_one.return_value = dict_response()
        sensor = JevSensor(
            observe=lambda: {"x": 1},
            questions=questions(),
            client=api,
            min_interval=60.0,
        )
        sensor.sense()
        now[0] += 61.0
        sensor.sense()
        assert api.system_one.call_count == 2

    def test_default_client(self, mocker, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
        mocker.patch.object(TypeSafeClient, "system_one", return_value=dict_response())
        sensor = JevSensor(observe=lambda: {}, questions=questions())
        assert sensor.sense()["danger"] == 0.87

    def test_default_client_sdk_response(self, mocker, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
        mocker.patch.object(TypeSafeClient, "system_one", return_value=sdk_response())
        sensor = JevSensor(observe=lambda: {}, questions=sdk_questions())
        assert sensor.sense()["threat"] == "hunting"

    def test_sensor_manager_integration(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = dict_response()
        sensor = JevSensor(observe=lambda: {}, questions=questions(), client=api)
        state = WorldState()
        SensorManager(sensors=[sensor]).update_state(state)
        assert state.danger == 0.87
        assert state.threat == "hunting"


class TestJevGoalStrategy:
    def goals(self):
        return [
            Goal(target_state={"fed": True}, priority=2, name="Eat"),
            Goal(target_state={"rested": True}, priority=1, name="Sleep"),
        ]

    def test_selects_jevs_choice_dict(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = {
            "answers": {"goal": {"type": "choice", "choice": "Sleep"}}
        }
        strategy = JevGoalStrategy(client=api)
        state = WorldState(fed=False, rested=False)

        selected = strategy.select(self.goals(), state)

        assert selected.name == "Sleep"
        (called_state, called_questions), _ = api.system_one.call_args
        assert called_state["world_state"] == {"fed": False, "rested": False}
        assert [g["name"] for g in called_state["goals"]] == ["Eat", "Sleep"]
        question = called_questions["goal"]
        assert isinstance(question, Choice)
        assert set(question.criteria) == {"Eat", "Sleep"}
        assert question.criteria["Sleep"]["target_state"] == {"rested": True}

    def test_selects_jevs_choice_sdk(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = SystemOneResponse(
            model="jev-latest",
            usage=usage(),
            answers={
                "goal": ChoiceAnswer(
                    choice="Sleep",
                    confidence=0.95,
                    probabilities={"Eat": 0.05, "Sleep": 0.95},
                )
            },
        )
        strategy = JevGoalStrategy(client=api)
        selected = strategy.select(self.goals(), WorldState(fed=False, rested=False))
        assert selected.name == "Sleep"

    def test_callable_target_state_json_safe(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = {
            "answers": {"goal": {"type": "choice", "choice": "Work"}}
        }
        goals = [Goal(target_state={"ready": lambda v: bool(v)}, name="Work")]
        selected = JevGoalStrategy(client=api).select(goals, WorldState(ready=False))
        assert selected.name == "Work"
        (_, called_questions), _ = api.system_one.call_args
        criteria = called_questions["goal"].criteria
        assert criteria["Work"]["target_state"] == {"ready": "<lambda>"}

    def test_empty_goals(self, mocker):
        api = mocker.Mock()
        assert JevGoalStrategy(client=api).select([], WorldState()) is None
        api.system_one.assert_not_called()

    def test_duplicate_names_rejected(self, mocker):
        goals = [
            Goal(target_state={"a": True}, name="Same"),
            Goal(target_state={"a": True}, name="Same"),
        ]
        with pytest.raises(ValueError, match="unique goal names"):
            JevGoalStrategy(client=mocker.Mock()).select(goals, WorldState())

    def test_unknown_choice_falls_back(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.return_value = {
            "answers": {"goal": {"type": "choice", "choice": "Ghost"}}
        }
        goals = self.goals()
        selected = JevGoalStrategy(client=api).select(goals, WorldState())
        assert selected is goals[0]
        assert "unknown goal" in caplog.text

    def test_unknown_choice_sdk_falls_back(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.return_value = SystemOneResponse(
            model="jev-latest",
            usage=usage(),
            answers={
                "goal": ChoiceAnswer(
                    choice="Ghost", confidence=0.5, probabilities={"Ghost": 1.0}
                )
            },
        )
        goals = self.goals()
        selected = JevGoalStrategy(client=api).select(goals, WorldState())
        assert selected is goals[0]
        assert "unknown goal" in caplog.text

    def test_missing_goal_answer_falls_back(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.return_value = {"answers": {}}
        goals = self.goals()
        selected = JevGoalStrategy(client=api).select(goals, WorldState())
        assert selected is goals[0]
        assert "falling back to first goal" in caplog.text

    def test_missing_answers_key_falls_back(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.return_value = {"model": "x"}
        goals = self.goals()
        selected = JevGoalStrategy(client=api).select(goals, WorldState())
        assert selected is goals[0]
        assert "falling back to first goal" in caplog.text

    def test_invalid_answers_shape_falls_back(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.return_value = {"answers": []}
        goals = self.goals()
        selected = JevGoalStrategy(client=api).select(goals, WorldState())
        assert selected is goals[0]
        assert "falling back to first goal" in caplog.text

    def test_unknown_answer_type_falls_back(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.return_value = {"answers": {"goal": {"type": "weird"}}}
        goals = self.goals()
        selected = JevGoalStrategy(client=api).select(goals, WorldState())
        assert selected is goals[0]
        assert "falling back to first goal" in caplog.text

    def test_api_error_falls_back(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.side_effect = TypeSafeError("down")
        goals = self.goals()
        selected = JevGoalStrategy(client=api).select(goals, WorldState())
        assert selected is goals[0]
        assert "falling back to first goal" in caplog.text

    def test_custom_instructions(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = {
            "answers": {"goal": {"type": "choice", "choice": "Eat"}}
        }
        strategy = JevGoalStrategy(client=api, instructions="Pick wisely.")
        strategy.select(self.goals(), WorldState())
        (_, called_questions), _ = api.system_one.call_args
        assert called_questions["goal"].instructions == "Pick wisely."

    def test_default_client(self, mocker, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
        mocker.patch.object(
            TypeSafeClient,
            "system_one",
            return_value={"answers": {"goal": {"type": "choice", "choice": "Eat"}}},
        )
        selected = JevGoalStrategy().select(self.goals(), WorldState())
        assert selected.name == "Eat"

    def test_arbitrator_integration(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = {
            "answers": {"goal": {"type": "choice", "choice": "Sleep"}}
        }
        done = Goal(target_state={"fed": True}, name="Eat")
        todo = Goal(target_state={"rested": True}, name="Sleep")
        arbitrator = GoalArbitrator(
            goals=[done, todo], strategy=JevGoalStrategy(client=api)
        )
        state = WorldState(fed=True, rested=False)

        selected = arbitrator.select_goal(state)

        assert selected.name == "Sleep"
        (_, called_questions), _ = api.system_one.call_args
        assert set(called_questions["goal"].criteria) == {"Sleep"}


class TestClientLifecycle:
    def test_sensor_default_client_uses_legacy_timeout(self, mocker):
        """A default-constructed client keeps the pre-SDK 30s timeout."""
        client_cls = mocker.patch("goapauto.models.jev.TypeSafeClient")
        JevSensor(observe=lambda: {}, questions=questions())
        client_cls.assert_called_once_with(timeout=30.0)

    def test_sensor_close_closes_owned_client(self, mocker):
        """close() releases the pool of a client the sensor created."""
        client_cls = mocker.patch("goapauto.models.jev.TypeSafeClient")
        sensor = JevSensor(observe=lambda: {}, questions=questions())
        sensor.close()
        client_cls.return_value.close.assert_called_once_with()

    def test_sensor_close_leaves_provided_client_open(self, mocker):
        """close() never closes a caller-provided client."""
        client = mocker.Mock()
        sensor = JevSensor(observe=lambda: {}, questions=questions(), client=client)
        sensor.close()
        client.close.assert_not_called()

    def test_strategy_default_client_uses_legacy_timeout(self, mocker):
        """A default-constructed client keeps the pre-SDK 30s timeout."""
        client_cls = mocker.patch("goapauto.models.jev.TypeSafeClient")
        JevGoalStrategy()
        client_cls.assert_called_once_with(timeout=30.0)

    def test_strategy_close_closes_owned_client(self, mocker):
        """close() releases the pool of a client the strategy created."""
        client_cls = mocker.patch("goapauto.models.jev.TypeSafeClient")
        strategy = JevGoalStrategy()
        strategy.close()
        client_cls.return_value.close.assert_called_once_with()

    def test_strategy_close_leaves_provided_client_open(self, mocker):
        """close() never closes a caller-provided client."""
        client = mocker.Mock()
        strategy = JevGoalStrategy(client=client)
        strategy.close()
        client.close.assert_not_called()
