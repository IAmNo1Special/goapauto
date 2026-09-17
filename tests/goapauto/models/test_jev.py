import io
import json
import urllib.error

import pytest

from goapauto.models.goal import Goal
from goapauto.models.goal_arbitrator import GoalArbitrator
from goapauto.models.jev import (
    JevGoalStrategy,
    JevSensor,
    TypeSafeClient,
    TypeSafeError,
)
from goapauto.models.sensors import SensorManager
from goapauto.models.worldstate import WorldState


class FakeHTTPResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=b""):
    return urllib.error.HTTPError(
        url="https://api.typesafe.ai/v1/systemone",
        code=code,
        msg="error",
        hdrs=None,
        fp=io.BytesIO(body),
    )


def unreadable_http_error(code):
    return urllib.error.HTTPError(
        url="https://api.typesafe.ai/v1/systemone",
        code=code,
        msg="error",
        hdrs=None,
        fp=None,
    )


class BrokenBody:
    def read(self):
        raise OSError("cannot read body")


@pytest.fixture
def client():
    return TypeSafeClient(api_key="test-key", max_retries=0)


@pytest.fixture
def urlopen(mocker):
    return mocker.patch("urllib.request.urlopen")


def system_one_response():
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


class TestTypeSafeClient:
    def test_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        with pytest.raises(TypeSafeError):
            TypeSafeClient()
        with pytest.raises(TypeSafeError):
            TypeSafeClient(api_key="   ")

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
        assert TypeSafeClient().api_key == "env-key"
        assert TypeSafeClient(api_key="explicit").api_key == "explicit"

    def test_env_base_url_and_model(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "k")
        monkeypatch.setenv("TYPESAFE_BASE_URL", "https://example.com/")
        monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "jev-2")
        client = TypeSafeClient()
        assert client.base_url == "https://example.com"
        assert client.model == "jev-2"

    def test_defaults(self):
        client = TypeSafeClient(api_key="k")
        assert client.base_url == "https://api.typesafe.ai"
        assert client.model == "jev-latest"

    def test_system_one_request(self, client, urlopen):
        urlopen.return_value = FakeHTTPResponse(system_one_response())
        state = {"health": 20}
        result = client.system_one(state, questions())

        assert result["model"] == "jev-latest"
        (request,) = urlopen.call_args.args
        assert request.full_url == "https://api.typesafe.ai/v1/systemone"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer test-key"
        body = json.loads(request.data.decode())
        assert body == {
            "state": state,
            "questions": questions(),
            "model": "jev-latest",
        }

    def test_list_models(self, client, urlopen):
        urlopen.return_value = FakeHTTPResponse([{"name": "jev-latest"}])
        assert client.list_models() == [{"name": "jev-latest"}]
        (request,) = urlopen.call_args.args
        assert request.full_url == "https://api.typesafe.ai/v1/models"
        assert request.get_method() == "GET"

    def test_list_models_unexpected_shape(self, client, urlopen):
        urlopen.return_value = FakeHTTPResponse({"name": "jev-latest"})
        with pytest.raises(TypeSafeError):
            client.list_models()

    def test_401(self, client, urlopen):
        urlopen.side_effect = http_error(401, b"nope")
        with pytest.raises(TypeSafeError, match="rejected the API key"):
            client.system_one({}, {})
        assert urlopen.call_count == 1

    def test_400_no_retry(self, client, urlopen):
        urlopen.side_effect = http_error(400, b"bad question")
        with pytest.raises(TypeSafeError, match="HTTP 400.*bad question"):
            client.system_one({}, {})
        assert urlopen.call_count == 1

    def test_429_retries_then_succeeds(self, mocker, urlopen):
        mocker.patch("time.sleep")
        client = TypeSafeClient(api_key="k", max_retries=1)
        urlopen.side_effect = [
            unreadable_http_error(429),
            FakeHTTPResponse(system_one_response()),
        ]
        assert client.system_one({}, {})["model"] == "jev-latest"
        assert urlopen.call_count == 2

    def test_500_exhausts_retries(self, mocker, urlopen):
        mocker.patch("time.sleep")
        client = TypeSafeClient(api_key="k", max_retries=1)
        urlopen.side_effect = unreadable_http_error(500)
        with pytest.raises(TypeSafeError, match="HTTP 500"):
            client.system_one({}, {})
        assert urlopen.call_count == 2

    def test_500_unreadable_body(self, client, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.typesafe.ai/v1/systemone",
            code=500,
            msg="error",
            hdrs=None,
            fp=BrokenBody(),
        )
        with pytest.raises(TypeSafeError, match="HTTP 500"):
            client.system_one({}, {})

    def test_connection_error(self, client, urlopen):
        urlopen.side_effect = urllib.error.URLError("boom")
        with pytest.raises(TypeSafeError, match="connection failed"):
            client.system_one({}, {})


class TestJevSensor:
    def test_sense_maps_answers(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = system_one_response()
        observation = {"enemies_nearby": 3, "health": 20}
        sensor = JevSensor(
            observe=lambda: observation, questions=questions(), client=api
        )

        assert sensor.sense() == {"danger": 0.87, "threat": "hunting", "hunger": 2.0}
        api.system_one.assert_called_once_with(observation, questions())
        # min_interval=0 re-senses on every call
        sensor.sense()
        assert api.system_one.call_count == 2

    def test_custom_mapping(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = system_one_response()
        sensor = JevSensor(
            observe=lambda: {},
            questions=questions(),
            mapping={"danger": "is_dangerous"},
            client=api,
        )
        assert sensor.sense() == {"is_dangerous": 0.87}

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

    def test_missing_answer(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.return_value = {"answers": {}}
        sensor = JevSensor(observe=lambda: {}, questions=questions(), client=api)
        assert sensor.sense() == {}
        assert "Missing answer" in caplog.text

    def test_api_error_first_call_returns_empty(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.side_effect = TypeSafeError("down")
        sensor = JevSensor(observe=lambda: {}, questions=questions(), client=api)
        assert sensor.sense() == {}
        assert "reusing last judgments" in caplog.text

    def test_api_error_reuses_cached(self, mocker, caplog):
        api = mocker.Mock()
        api.system_one.side_effect = [
            system_one_response(),
            TypeSafeError("down"),
        ]
        sensor = JevSensor(observe=lambda: {"x": 1}, questions=questions(), client=api)
        assert sensor.sense()["danger"] == 0.87
        assert sensor.sense()["danger"] == 0.87
        assert "reusing last judgments" in caplog.text

    def test_min_interval_skips_call(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = system_one_response()
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
        api.system_one.return_value = system_one_response()
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
        api.system_one.return_value = system_one_response()
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
        api.system_one.return_value = system_one_response()
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
        mocker.patch.object(
            TypeSafeClient, "system_one", return_value=system_one_response()
        )
        sensor = JevSensor(observe=lambda: {}, questions=questions())
        assert sensor.sense()["danger"] == 0.87

    def test_sensor_manager_integration(self, mocker):
        api = mocker.Mock()
        api.system_one.return_value = system_one_response()
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

    def test_selects_jevs_choice(self, mocker):
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
        assert question["type"] == "choice"
        assert set(question["criteria"]) == {"Eat", "Sleep"}
        assert question["criteria"]["Sleep"]["target_state"] == {"rested": True}

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
        assert called_questions["goal"]["instructions"] == "Pick wisely."

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
        assert set(called_questions["goal"]["criteria"]) == {"Sleep"}
