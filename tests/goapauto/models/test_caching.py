"""Tests for sensor caching and stale-data behavior (design 02).

Covers the shared cache types in ``goapauto.models.caching`` (CachePolicy,
CachingSensor, Staleness, ValueMeta, SensorReport, SensorHealth, AsyncSensor,
max_age, oldest_staleness), the Sensor/SensorManager coordination surface,
and the JevSensor/JudgmentSensor migrations onto the shared cache.

Time is a scripted monkeypatched clock -- never real sleeps.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

import pytest
from typesafe_sdk import Choice, Noul, Score, TypeSafeError

from goapauto.models.caching import (
    AsyncSensor,
    CachePolicy,
    CachingSensor,
    SensorHealth,
    SensorReport,
    Staleness,
    StalePolicy,
    ValueMeta,
    max_age,
    oldest_staleness,
)
from goapauto.models.jev import JevCallRecord, JevSensor
from goapauto.models.judgment import (
    FloatAnswer,
    JudgmentError,
    JudgmentResponse,
    JudgmentSensor,
    NoulQuestion,
    TokenUsage,
)
from goapauto.models.sensors import Sensor, SensorManager
from goapauto.models.worldstate import WorldState
from goapauto.testing import FakeTypeSafeClient


@pytest.fixture
def clock(monkeypatch):
    """Scripted monotonic clock. Tests set ``clock[0]`` to advance time."""
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    return now


class ScriptedSensor(Sensor):
    """Plain sensor with a scripted output / failure sequence."""

    def __init__(self, output: dict[str, Any] | None = None) -> None:
        self.output: dict[str, Any] | None = output
        self.failure: BaseException | None = None
        self.calls = 0

    def sense(self) -> dict[str, Any]:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        assert self.output is not None
        return dict(self.output)


def jev_questions():
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


def make_jev_sensor(clock, **kwargs):
    client = FakeTypeSafeClient(
        answers={"danger": 0.5, "threat": "hunting", "hunger": 1.0}
    )
    kwargs.setdefault("observe", lambda: {"scene": "woods"})
    kwargs.setdefault("questions", jev_questions())
    kwargs.setdefault("client", client)
    sensor = JevSensor(**kwargs)
    return sensor, client


def make_judgment_sensor(**kwargs):
    class ScriptedJudge:
        backend = "scripted"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.behavior: str = "ok"

        def judge(self, state, questions):
            self.calls.append(dict(state))
            if self.behavior == "ok":
                return JudgmentResponse(
                    answers={name: FloatAnswer(0.5) for name in questions},
                    backend=self.backend,
                    usage=TokenUsage(input_tokens=1, output_tokens=1),
                )
            raise JudgmentError("boom", backend=self.backend, retryable=True)

        def close(self) -> None:
            pass

    judge = ScriptedJudge()
    kwargs.setdefault("judge", judge)
    kwargs.setdefault("observe", lambda: {"scene": "woods"})
    kwargs.setdefault(
        "questions",
        {"a": NoulQuestion(instructions="a?"), "b": NoulQuestion(instructions="b?")},
    )
    sensor = JudgmentSensor(**kwargs)
    return sensor, judge


class TestCachePolicyValidation:
    def test_defaults(self):
        policy = CachePolicy()
        assert policy.stale_after is None
        assert policy.max_stale == 30.0
        assert policy.stale_policy is StalePolicy.KEEP_FLAGGED

    def test_stale_after_above_max_stale_rejected(self):
        with pytest.raises(ValueError, match="stale_after"):
            CachePolicy(stale_after=40.0, max_stale=30.0)

    def test_negative_stale_after_rejected(self):
        with pytest.raises(ValueError, match="stale_after"):
            CachePolicy(stale_after=-1.0)

    def test_negative_max_stale_rejected(self):
        with pytest.raises(ValueError, match="max_stale"):
            CachePolicy(max_stale=-0.5)

    def test_boundary_values_accepted(self):
        assert CachePolicy(stale_after=0.0, max_stale=30.0).stale_after == 0.0
        assert CachePolicy(stale_after=30.0, max_stale=30.0).stale_after == 30.0
        assert CachePolicy(max_stale=0.0).max_stale == 0.0

    def test_jev_sensor_validates_like_policy(self):
        _, client = make_jev_sensor(None)
        with pytest.raises(ValueError, match="stale_after"):
            JevSensor(
                observe=lambda: {},
                questions=jev_questions(),
                client=client,
                stale_after=40.0,
                max_stale=30.0,
            )
        with pytest.raises(ValueError, match="stale_after"):
            JevSensor(
                observe=lambda: {},
                questions=jev_questions(),
                client=client,
                stale_after=-1.0,
            )

    def test_judgment_sensor_validates_like_policy(self):
        sensor, _ = make_judgment_sensor()
        assert sensor is not None
        with pytest.raises(ValueError, match="stale_after"):
            make_judgment_sensor(stale_after=40.0, max_stale=30.0)


class FailingScriptedSensor(ScriptedSensor):
    pass


class TestLadderBands:
    """§3.1 ladder transitions at exact boundaries, both policies."""

    def test_exact_boundaries_keep_flagged(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(
            inner, policy=CachePolicy(stale_after=5.0, max_stale=30.0)
        )
        assert sensor.sense() == {"a": 1}
        inner.failure = RuntimeError("boom")

        clock[0] = 1005.0  # age == stale_after -> FRESH
        report = sensor.sense_detailed()
        assert report.meta["a"].staleness is Staleness.FRESH
        assert report.values == {"a": 1}

        clock[0] = 1005.0 + 1e-6  # age == stale_after + eps -> STALE
        report = sensor.sense_detailed()
        assert report.meta["a"].staleness is Staleness.STALE
        assert report.values == {"a": 1}

        clock[0] = 1030.0  # age == max_stale -> STALE, still served
        report = sensor.sense_detailed()
        assert report.meta["a"].staleness is Staleness.STALE
        assert report.values == {"a": 1}

        clock[0] = 1030.0 + 1e-6  # age > max_stale -> DEAD: fail-loud
        with pytest.raises(RuntimeError, match="boom"):
            sensor.sense()
        # The raising call produces no report; the last successful one stands.
        report = sensor.last_report()
        assert report is not None
        assert report.values == {"a": 1}
        assert set(report.meta) == set(report.values)

    def test_drop_policy_omits_stale_band(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(
            inner,
            policy=CachePolicy(
                stale_after=5.0, max_stale=30.0, stale_policy=StalePolicy.DROP
            ),
        )
        assert sensor.sense() == {"a": 1}
        inner.failure = RuntimeError("boom")

        clock[0] = 1010.0  # STALE band under DROP -> omitted like a cliff
        with pytest.raises(RuntimeError, match="boom"):
            sensor.sense()
        # last_report still reflects the last successful read
        report = sensor.last_report()
        assert report is not None
        assert report.values == {"a": 1}

    def test_default_mode_has_no_stale_band(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner)  # stale_after=None -> 0.5.0 cliff
        assert sensor.sense() == {"a": 1}
        inner.failure = RuntimeError("boom")

        clock[0] = 1010.0  # age 10 <= max_stale -> served, band FRESH
        report = sensor.sense_detailed()
        assert report.meta["a"].staleness is Staleness.FRESH
        assert report.values == {"a": 1}

        clock[0] = 1030.0 + 1e-6  # past max_stale -> DEAD: fail-loud
        with pytest.raises(RuntimeError, match="boom"):
            sensor.sense()
        assert sensor.last_report() is not None
        assert sensor.last_report().values == {"a": 1}

    def test_stale_after_zero_flags_everything(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner, policy=CachePolicy(stale_after=0.0))
        assert sensor.sense() == {"a": 1}
        inner.failure = RuntimeError("boom")
        clock[0] = 1000.0 + 1e-9
        report = sensor.sense_detailed()
        assert report.meta["a"].staleness is Staleness.STALE

    def test_max_stale_zero_propagates_first_failure(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner, policy=CachePolicy(max_stale=0.0))
        assert sensor.sense() == {"a": 1}
        failure = RuntimeError("boom")
        inner.failure = failure
        clock[0] = 1000.0 + 1e-6  # any age > max_stale=0 -> DEAD
        with pytest.raises(RuntimeError) as excinfo:
            sensor.sense()
        assert excinfo.value is failure

    def test_meta_fields_populated(self, clock):
        inner = ScriptedSensor({"a": 1, "b": 2})
        sensor = CachingSensor(inner, name="vision")
        report = sensor.sense_detailed()
        assert set(report.meta) == {"a", "b"}
        meta = report.meta["a"]
        assert isinstance(meta, ValueMeta)
        assert meta.age_seconds == pytest.approx(0.0)
        assert meta.staleness is Staleness.FRESH
        assert meta.source == "vision"
        assert meta.as_of_monotonic == pytest.approx(1000.0)
        assert report.sensor == "vision"

    def test_failure_path_source_is_cache(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner, name="vision")
        assert sensor.sense() == {"a": 1}
        inner.failure = RuntimeError("boom")
        clock[0] = 1010.0
        report = sensor.sense_detailed()
        assert report.meta["a"].source == "cache"
        assert report.meta["a"].age_seconds == pytest.approx(10.0)

    def test_name_falls_back_to_inner_type(self):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner)
        assert sensor.sense_detailed().sensor == "ScriptedSensor"
        assert sensor.health().name == "ScriptedSensor"

    def test_sense_return_has_no_metadata_keys(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(
            inner, policy=CachePolicy(stale_after=5.0, max_stale=30.0)
        )
        assert sensor.sense() == {"a": 1}
        inner.failure = RuntimeError("boom")
        clock[0] = 1010.0
        values = sensor.sense()
        assert values == {"a": 1}
        assert all(not isinstance(v, ValueMeta) for v in values.values())


class TestPerKeyIndependence:
    def test_keys_age_independently(self, clock):
        inner = ScriptedSensor({"a": 1, "b": 2})
        sensor = CachingSensor(
            inner, policy=CachePolicy(stale_after=5.0, max_stale=30.0)
        )
        assert sensor.sense() == {"a": 1, "b": 2}

        clock[0] = 1003.0
        inner.output = {"a": 1}  # "b" absent: keeps its old (value, as_of)
        assert sensor.sense() == {"a": 1, "b": 2}

        clock[0] = 1007.0
        inner.failure = RuntimeError("boom")
        report = sensor.sense_detailed()
        # "a" re-sensed at t=1003 (age 4, FRESH); "b" untouched since t=1000
        # (age 7, STALE).
        assert report.meta["a"].staleness is Staleness.FRESH
        assert report.meta["b"].staleness is Staleness.STALE
        assert report.values == {"a": 1, "b": 2}

    def test_absent_key_keeps_aging_toward_dead(self, clock):
        """§3.3 write rule: absent keys are not refreshed and not deleted."""
        inner = ScriptedSensor({"a": 1, "b": 2})
        sensor = CachingSensor(
            inner, policy=CachePolicy(stale_after=5.0, max_stale=30.0)
        )
        sensor.sense()
        inner.output = {"a": 1}  # "b" gated-out/absent from now on
        clock[0] = 1010.0
        sensor.sense()
        # "b" was not refreshed at t=1010: still aging from t=1000.
        assert sensor.last_report().meta["b"].as_of_monotonic == pytest.approx(1000.0)
        assert sensor.last_report().meta["b"].staleness is Staleness.STALE

        clock[0] = 1031.0
        inner.failure = RuntimeError("boom")
        report = sensor.sense_detailed()
        assert "b" not in report.values  # DEAD: omitted
        assert "b" not in report.meta
        assert report.values == {"a": 1}
        # ...but not deleted: a later success refreshes it again.
        inner.failure = None
        inner.output = {"a": 1, "b": 9}
        assert sensor.sense() == {"a": 1, "b": 9}

    def test_successful_call_never_deletes_entries(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner, name="s")
        sensor.sense()
        inner.output = {}
        sensor.sense()
        # "a" survives an empty successful output, aging honestly.
        assert sensor.last_report().values == {"a": 1}
        health = sensor.health()
        assert health.stale_keys == () and health.dead_keys == ()
        clock[0] = 1040.0
        assert sensor.health().dead_keys == ("a",)


class TestFailLoud:
    def test_first_call_failure_propagates_original(self):
        failure = RuntimeError("boom")
        inner = ScriptedSensor()
        inner.failure = failure
        sensor = CachingSensor(inner)
        with pytest.raises(RuntimeError) as excinfo:
            sensor.sense()
        assert excinfo.value is failure

    def test_all_dead_propagates_original(self, clock):
        failure = RuntimeError("boom")
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner, policy=CachePolicy(max_stale=10.0))
        sensor.sense()
        inner.failure = failure
        clock[0] = 1020.0 + 1e-6  # "a" DEAD: no usable cache
        with pytest.raises(RuntimeError) as excinfo:
            sensor.sense()
        assert excinfo.value is failure

    def test_warm_cache_serves_on_failure(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner)
        sensor.sense()
        inner.failure = RuntimeError("boom")
        clock[0] = 1010.0
        assert sensor.sense() == {"a": 1}
        assert sensor.health().error == "RuntimeError"

    def test_error_cleared_on_next_success(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner)
        sensor.sense()
        inner.failure = RuntimeError("boom")
        clock[0] = 1010.0
        sensor.sense()
        assert sensor.health().error == "RuntimeError"
        inner.failure = None
        sensor.sense()
        assert sensor.health().error is None

    def test_base_exception_not_swallowed(self):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner)
        sensor.sense()
        inner.failure = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            sensor.sense()


class TestOnChange:
    def test_unchanged_output_does_not_refresh_ttl(self, clock):
        inner = ScriptedSensor({"a": 1.0})
        sensor = CachingSensor(
            inner,
            policy=CachePolicy(stale_after=5.0, max_stale=30.0),
            on_change=lambda output: round(output["a"]),
        )
        sensor.sense()
        clock[0] = 1003.0
        inner.output = {"a": 1.04}  # semantically unchanged: fingerprint equal
        sensor.sense()
        # as_of not refreshed: still aging from t=1000.
        assert sensor.last_report().meta["a"].as_of_monotonic == pytest.approx(1000.0)
        clock[0] = 1007.0
        inner.failure = RuntimeError("boom")
        report = sensor.sense_detailed()
        assert report.meta["a"].staleness is Staleness.STALE

    def test_changed_output_refreshes_ttl(self, clock):
        inner = ScriptedSensor({"a": 1.0})
        sensor = CachingSensor(
            inner,
            policy=CachePolicy(stale_after=5.0, max_stale=30.0),
            on_change=lambda output: round(output["a"]),
        )
        sensor.sense()
        clock[0] = 1003.0
        inner.output = {"a": 2.0}
        sensor.sense()
        assert sensor.last_report().meta["a"].as_of_monotonic == pytest.approx(1003.0)

    def test_raising_on_change_propagates(self):
        def bad_fingerprint(output):
            raise ValueError("broken detector")

        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner, on_change=bad_fingerprint)
        with pytest.raises(ValueError, match="broken detector"):
            sensor.sense()

    def test_unchanged_output_writes_new_keys(self, clock):
        # Coarse fingerprint: output "unchanged" but a new key appears.
        # write_new adds the new key without refreshing existing ones.
        inner = ScriptedSensor({"a": 1.0})
        sensor = CachingSensor(
            inner,
            policy=CachePolicy(stale_after=5.0, max_stale=30.0),
            on_change=lambda output: round(output["a"]),
        )
        sensor.sense()
        clock[0] = 1003.0
        inner.output = {"a": 1.04, "b": 2.0}  # fingerprint still 1
        values = sensor.sense()
        assert values == {"a": 1.0, "b": 2.0}
        # "a" kept its original as_of; "b" is new.
        assert sensor.last_report().meta["a"].as_of_monotonic == pytest.approx(1000.0)
        assert sensor.last_report().meta["b"].as_of_monotonic == pytest.approx(1003.0)

    def test_diagnostics_malformed_report_returns_empty(self):
        from goapauto.models.caching import _PerKeyCache

        cache = _PerKeyCache(CachePolicy())
        bad_report = SensorReport(values={}, meta={"a": None}, sensor="x")
        assert cache.diagnostics(bad_report, 1000.0) == {}

    def test_generic_sensor_diagnostics(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(
            inner, policy=CachePolicy(stale_after=5.0, max_stale=30.0)
        )
        sensor.sense()
        diagnostics = sensor.diagnostics()
        assert set(diagnostics) == {
            "data_age_seconds",
            "staleness",
            "dead_keys",
            "worst_staleness",
        }
        assert diagnostics["worst_staleness"] == "fresh"
        json.dumps(diagnostics)

    def test_default_none_refreshes_every_success(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner, policy=CachePolicy(stale_after=5.0))
        sensor.sense()
        clock[0] = 1003.0
        sensor.sense()
        assert sensor.last_report().meta["a"].as_of_monotonic == pytest.approx(1003.0)


class TestLastReportLifecycle:
    def test_none_before_first_sense(self):
        sensor = CachingSensor(ScriptedSensor({"a": 1}))
        assert sensor.last_report() is None

    def test_reflects_most_recent_sense(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner)
        sensor.sense()
        first = sensor.last_report()
        clock[0] = 1005.0
        sensor.sense()
        second = sensor.last_report()
        assert second is not first
        assert second.meta["a"].as_of_monotonic == pytest.approx(1005.0)

    def test_sense_detailed_returns_last_report(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(inner)
        report = sensor.sense_detailed()
        assert report is sensor.last_report()
        assert report.values == {"a": 1}

    def test_sense_detailed_propagates_when_no_cache(self):
        inner = ScriptedSensor()
        inner.failure = RuntimeError("boom")
        sensor = CachingSensor(inner)
        with pytest.raises(RuntimeError):
            sensor.sense_detailed()
        assert sensor.last_report() is None


class TestJevSensorCacheSurface:
    def test_last_report_none_before_first_sense(self, clock):
        sensor, _ = make_jev_sensor(clock)
        assert sensor.last_report() is None
        # No cache data before the first report; gating keys (04) still present.
        assert "data_age_seconds" not in sensor.diagnostics()

    def test_sense_detailed_matches_sense(self, clock):
        sensor, _ = make_jev_sensor(clock)
        values = sensor.sense()
        report = sensor.sense_detailed()
        assert report.values == values
        assert set(report.meta) == set(values)
        assert report.sensor == "JevSensor"
        for meta in report.meta.values():
            assert meta.staleness is Staleness.FRESH
            assert meta.source == "JevSensor"
            assert meta.as_of_monotonic == pytest.approx(1000.0)

    def test_named_sensor_reports_name(self, clock):
        sensor, _ = make_jev_sensor(clock, name="threat-judge")
        report = sensor.sense_detailed()
        assert report.sensor == "threat-judge"
        assert sensor.health().name == "threat-judge"
        assert all(m.source == "threat-judge" for m in report.meta.values())

    def test_health_reports_bands(self, clock):
        sensor, _ = make_jev_sensor(
            clock,
            stale_after=5.0,
            max_stale=30.0,
            name="j",
        )
        sensor.sense()
        health = sensor.health()
        assert health.name == "j"
        assert health.status == "fresh"
        assert health.last_success_age == pytest.approx(0.0)
        assert health.stale_keys == () and health.dead_keys == ()
        assert health.error is None

        failing = FakeTypeSafeClient(
            responder=lambda state, questions: (_ for _ in ()).throw(
                TypeSafeError("boom")
            )
        )
        sensor._client = failing
        clock[0] = 1010.0
        report = sensor.sense_detailed()
        assert all(m.staleness is Staleness.STALE for m in report.meta.values())
        assert all(m.source == "cache" for m in report.meta.values())
        health = sensor.health()
        assert health.status == "stale"
        assert set(health.stale_keys) == {"danger", "threat", "hunger"}
        assert health.dead_keys == ()
        assert health.error == "TypeSafeError"
        assert health.last_success_age == pytest.approx(10.0)

    def test_health_dead_keys(self, clock):
        sensor, _ = make_jev_sensor(clock, stale_after=5.0, max_stale=30.0)
        sensor.sense()
        failing = FakeTypeSafeClient(
            responder=lambda state, questions: (_ for _ in ()).throw(
                TypeSafeError("boom")
            )
        )
        sensor._client = failing
        clock[0] = 1040.0
        assert sensor.sense() == {}
        health = sensor.health()
        assert health.status == "dead"
        assert set(health.dead_keys) == {"danger", "threat", "hunger"}
        assert health.stale_keys == ()

    def test_health_never_sensed(self, clock):
        sensor, _ = make_jev_sensor(clock)
        health = sensor.health()
        assert health.status == "fresh"
        assert health.last_success_age is None
        assert health.error is None

    def test_judge_shares_ladder_with_injection_source(self, clock):
        sensor, _ = make_jev_sensor(clock, name="j")
        result = sensor.judge({"scene": "cave"})
        assert result == {"danger": 0.5, "threat": "hunting", "hunger": 1.0}
        report = sensor.last_report()
        assert report is not None
        assert all(m.source == "j" for m in report.meta.values())
        assert all(
            m.as_of_monotonic == pytest.approx(1000.0) for m in report.meta.values()
        )

    def test_diagnostics_shape(self, clock):
        sensor, _ = make_jev_sensor(clock, stale_after=5.0, max_stale=30.0)
        sensor.sense()
        clock[0] = 1010.0
        failing = FakeTypeSafeClient(
            responder=lambda state, questions: (_ for _ in ()).throw(
                TypeSafeError("boom")
            )
        )
        sensor._client = failing
        sensor.sense()
        diagnostics = sensor.diagnostics()
        # 02 caching keys present (04 gating keys merge into the same mapping).
        assert {
            "data_age_seconds",
            "staleness",
            "dead_keys",
            "worst_staleness",
        } <= set(diagnostics)
        assert diagnostics["staleness"] == {
            "danger": "stale",
            "threat": "stale",
            "hunger": "stale",
        }
        assert diagnostics["data_age_seconds"]["danger"] == pytest.approx(10.0)
        assert diagnostics["dead_keys"] == []
        assert diagnostics["worst_staleness"] == "stale"
        json.dumps(diagnostics)  # JSON-safe

    def test_diagnostics_dead_keys_listed(self, clock):
        sensor, _ = make_jev_sensor(clock, stale_after=5.0, max_stale=30.0)
        sensor.sense()
        failing = FakeTypeSafeClient(
            responder=lambda state, questions: (_ for _ in ()).throw(
                TypeSafeError("boom")
            )
        )
        sensor._client = failing
        clock[0] = 1040.0
        sensor.sense()
        diagnostics = sensor.diagnostics()
        assert sorted(diagnostics["dead_keys"]) == ["danger", "hunger", "threat"]
        assert diagnostics["worst_staleness"] == "dead"
        assert diagnostics["staleness"] == {}
        assert diagnostics["data_age_seconds"] == {}

    def test_diagnostics_never_raises(self, clock):
        sensor, _ = make_jev_sensor(clock)
        sensor.sense()
        sensor._last_report = object()  # corrupt: must still not raise
        diagnostics = sensor.diagnostics()
        assert diagnostics["gating_enabled"] is False
        assert "data_age_seconds" not in diagnostics
        json.dumps(diagnostics)


class TestJevSensorFingerprint:
    def test_semantic_fingerprint_suppresses_resense(self, clock, mocker):
        client = FakeTypeSafeClient(
            answers={"danger": 0.5, "threat": "hunting", "hunger": 1.0}
        )
        observations = [{"x": 0.30000000000000004}, {"x": 0.3}, {"x": 0.3}]
        sensor = JevSensor(
            observe=lambda: observations.pop(0),
            questions=jev_questions(),
            client=client,
            min_interval=60.0,
            observation_fingerprint=lambda obs: round(obs["x"], 6),
        )
        sensor.sense()
        sensor.sense()
        sensor.sense()
        assert len(client.calls) == 1

    def test_raising_fingerprint_propagates(self, clock):
        def bad_fingerprint(obs):
            raise ValueError("broken detector")

        sensor, _ = make_jev_sensor(
            clock, observation_fingerprint=bad_fingerprint, min_interval=60.0
        )
        sensor.sense()
        with pytest.raises(ValueError, match="broken detector"):
            sensor.sense()

    def test_fingerprint_ignored_when_resense_on_change_false(self, clock):
        seen = []

        def counting_fingerprint(obs):
            seen.append(obs)
            return obs

        sensor, client = make_jev_sensor(
            clock,
            observation_fingerprint=counting_fingerprint,
            resense_on_change=False,
            min_interval=60.0,
        )
        sensor._observe = lambda: {"n": len(seen)}
        sensor.sense()
        sensor.sense()
        assert seen == []  # never consulted
        assert len(client.calls) == 1

    def test_nan_observation_resenses_every_call(self, clock):
        sensor, client = make_jev_sensor(clock, min_interval=3600.0)
        sensor._observe = lambda: {"x": math.nan}
        sensor.sense()
        sensor.sense()
        # Documented NaN caveat: NaN != NaN, so change detection always fires.
        assert len(client.calls) == 2

    def test_changed_observation_resenses_without_fingerprint(self, clock):
        observations = [{"x": 1}, {"x": 2}]
        sensor, client = make_jev_sensor(clock, min_interval=3600.0)
        sensor._observe = lambda: observations.pop(0)
        sensor.sense()
        sensor.sense()
        assert len(client.calls) == 2

    def test_fingerprint_change_resenses(self, clock):
        observations = [{"x": 1}, {"x": 2}]
        sensor, client = make_jev_sensor(
            clock,
            min_interval=3600.0,
            observation_fingerprint=lambda obs: obs["x"],
        )
        sensor._observe = lambda: observations.pop(0)
        sensor.sense()
        sensor.sense()
        assert len(client.calls) == 2


class TestJevSensorOptInLadder:
    def test_skip_path_serves_stale_without_counter(self, clock):
        records = []
        sensor, _ = make_jev_sensor(
            clock,
            stale_after=5.0,
            min_interval=60.0,
            telemetry=records.append,
        )
        assert sensor.sense() == {
            "danger": 0.5,
            "threat": "hunting",
            "hunger": 1.0,
        }
        clock[0] = 1010.0  # skip path: interval not elapsed
        values = sensor.sense()
        assert values == {"danger": 0.5, "threat": "hunting", "hunger": 1.0}
        report = sensor.last_report()
        assert all(m.staleness is Staleness.STALE for m in report.meta.values())
        assert sensor.stats().stale_cache_hits == 0
        assert len(records) == 1  # skip path emits no new telemetry

    def test_failure_path_replay_counts_and_flags(self, clock):
        records = []
        sensor, _ = make_jev_sensor(
            clock, stale_after=5.0, max_stale=30.0, telemetry=records.append
        )
        sensor.sense()
        failing = FakeTypeSafeClient(
            responder=lambda state, questions: (_ for _ in ()).throw(
                TypeSafeError("boom")
            )
        )
        sensor._client = failing
        clock[0] = 1010.0
        assert sensor.sense() == {
            "danger": 0.5,
            "threat": "hunting",
            "hunger": 1.0,
        }
        assert sensor.stats().stale_cache_hits == 1
        assert records[-1].stale_cache_hit is True
        assert records[-1].staleness == "stale"

    def test_drop_policy_failure_path(self, clock):
        sensor, _ = make_jev_sensor(
            clock,
            stale_after=5.0,
            max_stale=30.0,
            stale_policy=StalePolicy.DROP,
        )
        sensor.sense()
        failing = FakeTypeSafeClient(
            responder=lambda state, questions: (_ for _ in ()).throw(
                TypeSafeError("boom")
            )
        )
        sensor._client = failing
        clock[0] = 1010.0
        assert sensor.sense() == {}  # STALE band dropped under DROP


class TestJevCallRecordStaleness:
    def test_default_is_fresh_and_positional_compat(self):
        record = JevCallRecord("sensor", 1.0, None, None, None, False)
        assert record.staleness == "fresh"

    def test_success_records_fresh(self, clock):
        records = []
        sensor, _ = make_jev_sensor(clock, telemetry=records.append)
        sensor.sense()
        assert records[0].staleness == "fresh"

    def test_failure_replay_default_mode_records_fresh(self, clock):
        records = []
        sensor, _ = make_jev_sensor(clock, telemetry=records.append)
        sensor.sense()
        failing = FakeTypeSafeClient(
            responder=lambda state, questions: (_ for _ in ()).throw(
                TypeSafeError("boom")
            )
        )
        sensor._client = failing
        clock[0] = 1010.0
        sensor.sense()
        assert records[-1].stale_cache_hit is True
        assert records[-1].staleness == "fresh"

    def test_failure_without_replay_records_dead(self, clock):
        records = []
        sensor, _ = make_jev_sensor(clock, max_stale=0.0, telemetry=records.append)
        sensor.sense()
        failing = FakeTypeSafeClient(
            responder=lambda state, questions: (_ for _ in ()).throw(
                TypeSafeError("boom")
            )
        )
        sensor._client = failing
        clock[0] = 1010.0
        assert sensor.sense() == {}
        assert records[-1].stale_cache_hit is False
        assert records[-1].staleness == "dead"

    def test_strategy_records_default_fresh(self, clock):
        from goapauto.models.jev import JevGoalStrategy

        records = []
        client = FakeTypeSafeClient(answers={"goal": "a"})
        strategy = JevGoalStrategy(client=client, telemetry=records.append)
        from goapauto.models.goal import Goal

        strategy.select([Goal(name="a", target_state={"x": True})], WorldState())
        assert records[0].staleness == "fresh"


class TestJudgmentSensorCacheSurface:
    def test_last_report_none_before_first_sense(self):
        sensor, _ = make_judgment_sensor()
        assert sensor.last_report() is None
        assert sensor.diagnostics() == {}

    def test_sense_detailed_and_health(self):
        sensor, judge = make_judgment_sensor(name="js")
        values = sensor.sense()
        assert values == {"a": 0.5, "b": 0.5}
        report = sensor.sense_detailed()
        assert set(report.meta) == {"a", "b"}
        assert report.sensor == "js"
        assert all(m.source == "js" for m in report.meta.values())
        health = sensor.health()
        assert health.name == "js"
        assert health.status == "fresh"
        assert health.error is None

    def test_opt_in_ladder_on_failure(self, clock):
        sensor, judge = make_judgment_sensor(stale_after=5.0, max_stale=30.0)
        sensor.sense()
        judge.behavior = "fail"
        clock[0] += 10.0
        report = sensor.sense_detailed()
        assert all(m.staleness is Staleness.STALE for m in report.meta.values())
        assert sensor.stats().stale_cache_hits == 1
        clock[0] += 25.0
        assert sensor.sense() == {}
        assert sensor.health().status == "dead"

    def test_diagnostics_shape(self):
        sensor, _ = make_judgment_sensor()
        sensor.sense()
        diagnostics = sensor.diagnostics()
        assert set(diagnostics) == {
            "data_age_seconds",
            "staleness",
            "dead_keys",
            "worst_staleness",
        }
        assert diagnostics["worst_staleness"] == "fresh"

    def test_fingerprint_change_resenses(self, clock):
        seen = []

        def counting_fingerprint(observation):
            seen.append(observation)
            return len(seen)

        sensor, judge = make_judgment_sensor(
            observation_fingerprint=counting_fingerprint,
            min_interval=60.0,
        )
        sensor._observe = lambda: {"n": len(seen)}
        sensor.sense()
        sensor.sense()
        assert len(judge.calls) == 2

    def test_nan_observation_resenses_every_call(self, clock):
        sensor, judge = make_judgment_sensor(min_interval=3600.0)
        sensor._observe = lambda: {"x": math.nan}
        sensor.sense()
        sensor.sense()
        assert len(judge.calls) == 2

    def test_nan_in_previous_observation_resenses(self, clock):
        observations = [{"x": math.nan}, {"x": 1.0}]
        sensor, judge = make_judgment_sensor(min_interval=3600.0)
        sensor._observe = lambda: observations.pop(0)
        sensor.sense()
        sensor.sense()
        assert len(judge.calls) == 2

    def test_name_defaults_to_class_name(self):
        sensor, _ = make_judgment_sensor()
        assert sensor.last_report() is None
        sensor.sense()
        assert sensor.last_report().sensor == "JudgmentSensor"

    def test_fingerprint_change_detection(self):
        observations = [{"x": 0.30000000000000004}, {"x": 0.3}]
        sensor, judge = make_judgment_sensor(
            observe=lambda: observations.pop(0),
            min_interval=60.0,
            observation_fingerprint=lambda obs: round(obs["x"], 6),
        )
        sensor.sense()
        sensor.sense()
        assert len(judge.calls) == 1


class TestSensorName:
    def test_default_none(self):
        assert ScriptedSensor({"a": 1}).name is None

    def test_settable(self):
        sensor = ScriptedSensor({"a": 1})
        sensor.name = "custom"
        assert sensor.name == "custom"


class TestUpdateStateDetailed:
    def test_merge_order_and_report_order(self, clock):
        s1 = CachingSensor(ScriptedSensor({"a": 1, "b": 1}), name="first")
        s2 = CachingSensor(ScriptedSensor({"b": 2, "c": 3}), name="second")
        manager = SensorManager([s1, s2])
        state = WorldState()
        reports = manager.update_state_detailed(state)
        assert state.to_dict() == {"a": 1, "b": 2, "c": 3}  # later wins
        assert [r.sensor for r in reports] == ["first", "second"]
        assert reports[0].values == {"a": 1, "b": 1}
        assert reports[1].values == {"b": 2, "c": 3}

    def test_plain_sensor_gets_synthesized_fresh_report(self, clock):
        plain = ScriptedSensor({"x": 9})
        manager = SensorManager([plain])
        state = WorldState()
        reports = manager.update_state_detailed(state)
        assert state.x == 9
        (report,) = reports
        assert report.values == {"x": 9}
        assert report.meta["x"].staleness is Staleness.FRESH
        assert report.meta["x"].source == "ScriptedSensor"
        assert report.sensor == "ScriptedSensor"

    def test_worldstate_not_polluted_by_stale(self, clock):
        inner = ScriptedSensor({"enemy_distance": 12.5})
        sensor = CachingSensor(
            inner, policy=CachePolicy(stale_after=5.0, max_stale=30.0)
        )
        manager = SensorManager([sensor])
        state = WorldState()
        manager.update_state_detailed(state)
        inner.failure = RuntimeError("boom")
        clock[0] = 1010.0
        reports = manager.update_state_detailed(state)
        assert reports[0].meta["enemy_distance"].staleness is Staleness.STALE
        assert state.to_dict() == {"enemy_distance": 12.5}
        assert state.diff(WorldState(enemy_distance=12.5)) == {}
        hash(state)  # does not raise

    def test_sensor_error_propagates_and_discards_partial(self, clock):
        good = CachingSensor(ScriptedSensor({"a": 1}), name="good")
        bad_inner = ScriptedSensor({"b": 2})
        bad_inner.failure = RuntimeError("boom")
        bad = CachingSensor(bad_inner, name="bad")
        manager = SensorManager([good, bad])
        state = WorldState()
        with pytest.raises(RuntimeError, match="boom"):
            manager.update_state_detailed(state)
        # No half-merged state: the good sensor's values were not written.
        assert state.to_dict() == {}

    def test_update_state_propagates_error(self):
        bad = ScriptedSensor({"b": 2})
        bad.failure = RuntimeError("boom")
        manager = SensorManager([bad])
        with pytest.raises(RuntimeError, match="boom"):
            manager.update_state(WorldState())


class TestManagerHealth:
    def test_mixed_sensors(self, clock):
        caching = CachingSensor(
            ScriptedSensor({"a": 1}),
            policy=CachePolicy(stale_after=5.0, max_stale=30.0),
            name="cached",
        )
        plain = ScriptedSensor({"b": 2})
        plain.name = "live"
        manager = SensorManager([caching, plain])
        manager.update_state(WorldState())

        (cached_health, plain_health) = manager.health()
        assert isinstance(cached_health, SensorHealth)
        assert cached_health.name == "cached"
        assert cached_health.status == "fresh"
        assert plain_health.name == "live"
        assert plain_health.status == "fresh"
        assert plain_health.last_success_age is None
        assert plain_health.stale_keys == () and plain_health.dead_keys == ()
        assert plain_health.error is None

    def test_raising_health_becomes_worst_case(self):
        class BrokenHealth(Sensor):
            name = "broken"

            def sense(self):
                return {}

            def health(self):
                raise RuntimeError("health exploded")

        manager = SensorManager([BrokenHealth()])
        (entry,) = manager.health()
        assert entry.name == "broken"
        assert entry.status == "dead"
        assert entry.last_success_age is None
        assert entry.stale_keys == () and entry.dead_keys == ()
        assert entry.error == "health_failed:RuntimeError"

    def test_health_never_raises(self):
        class AlwaysBroken(Sensor):
            def sense(self):
                return {}

            def health(self):
                raise AssertionError("nope")

        manager = SensorManager([AlwaysBroken(), AlwaysBroken()])
        assert len(manager.health()) == 2  # did not raise

    def test_health_in_sensor_order(self):
        s1 = ScriptedSensor({"a": 1})
        s1.name = "one"
        s2 = ScriptedSensor({"b": 2})
        s2.name = "two"
        manager = SensorManager([s1, s2])
        assert [h.name for h in manager.health()] == ["one", "two"]


class FakeAsyncSensor:
    """Minimal AsyncSensor for the slim async seam."""

    name = "async-sensor"

    def __init__(self, output):
        self._output = output
        self.calls = 0

    async def asense(self):
        self.calls += 1
        return dict(self._output)

    async def asense_detailed(self):
        values = await self.asense()
        return SensorReport(values=values, meta={}, sensor=self.name)


class AsyncOnlySensor:
    name = "async-only"

    def __init__(self, output):
        self._output = output

    async def asense(self):
        return dict(self._output)


class TestAsyncSeam:
    async def test_aupdate_state_with_async_sensor(self):
        sensor = FakeAsyncSensor({"a": 1})
        manager = SensorManager([sensor])
        state = WorldState()
        result = await manager.aupdate_state(state)
        assert result is state
        assert state.a == 1
        assert sensor.calls == 1

    async def test_aupdate_state_calls_sync_inline(self):
        sync = ScriptedSensor({"b": 2})
        manager = SensorManager([FakeAsyncSensor({"a": 1}), sync])
        state = WorldState()
        await manager.aupdate_state(state)
        assert state.to_dict() == {"a": 1, "b": 2}
        assert sync.calls == 1

    async def test_aupdate_state_neither_raises_type_error(self):
        manager = SensorManager([object()])
        with pytest.raises(TypeError, match=r"neither asense\(\) nor sense\(\)"):
            await manager.aupdate_state(WorldState())

    async def test_aupdate_state_detailed_prefers_asense_detailed(self):
        sensor = FakeAsyncSensor({"a": 1})
        manager = SensorManager([sensor])
        reports = await manager.aupdate_state_detailed(WorldState())
        (report,) = reports
        assert report.values == {"a": 1}
        assert report.sensor == "async-sensor"

    async def test_aupdate_state_detailed_synthesizes_from_asense(self):
        sensor = AsyncOnlySensor({"a": 5})
        manager = SensorManager([sensor])
        state = WorldState()
        reports = await manager.aupdate_state_detailed(state)
        (report,) = reports
        assert report.values == {"a": 5}
        assert report.meta["a"].staleness is Staleness.FRESH
        assert state.a == 5

    async def test_aupdate_state_detailed_sync_sensors(self, clock):
        caching = CachingSensor(ScriptedSensor({"a": 1}), name="c")
        plain = ScriptedSensor({"b": 2})
        manager = SensorManager([caching, plain])
        state = WorldState()
        reports = await manager.aupdate_state_detailed(state)
        assert [r.sensor for r in reports] == ["c", "ScriptedSensor"]
        assert state.to_dict() == {"a": 1, "b": 2}

    async def test_aupdate_state_detailed_neither_raises_type_error(self):
        manager = SensorManager([object()])
        with pytest.raises(TypeError, match=r"neither asense\(\) nor sense\(\)"):
            await manager.aupdate_state_detailed(WorldState())

    async def test_async_sensor_protocol_runtime_checkable(self):
        assert isinstance(FakeAsyncSensor({"a": 1}), AsyncSensor)
        assert not isinstance(ScriptedSensor({"a": 1}), AsyncSensor)

    async def test_async_failure_propagates(self):
        class FailingAsync:
            name = "failing"

            async def asense(self):
                raise RuntimeError("async boom")

        manager = SensorManager([FailingAsync()])
        with pytest.raises(RuntimeError, match="async boom"):
            await manager.aupdate_state(WorldState())


class TestAggregationHelpers:
    def _report(self, bands: dict[str, Staleness], ages: dict[str, float]):
        return SensorReport(
            values={k: k for k in bands},
            meta={
                k: ValueMeta(
                    age_seconds=ages[k],
                    staleness=bands[k],
                    source="cache",
                    as_of_monotonic=0.0,
                )
                for k in bands
            },
            sensor="s",
        )

    def test_max_age_none_when_no_keys(self):
        assert max_age([]) is None
        assert max_age([self._report({}, {})]) is None

    def test_max_age_oldest_across_reports(self):
        r1 = self._report({"a": Staleness.FRESH}, {"a": 1.0})
        r2 = self._report({"b": Staleness.STALE}, {"b": 7.5})
        assert max_age([r1, r2]) == pytest.approx(7.5)

    def test_oldest_staleness_fresh_when_empty(self):
        assert oldest_staleness([]) is Staleness.FRESH
        assert oldest_staleness([self._report({}, {})]) is Staleness.FRESH

    def test_oldest_staleness_worst_band_ordering(self):
        fresh = self._report({"a": Staleness.FRESH}, {"a": 1.0})
        stale = self._report({"b": Staleness.STALE}, {"b": 2.0})
        assert oldest_staleness([fresh, stale]) is Staleness.STALE
        assert oldest_staleness([stale, fresh]) is Staleness.STALE
        assert oldest_staleness([fresh]) is Staleness.FRESH

    def test_pure_functions_of_report_list(self):
        r = self._report({"a": Staleness.STALE}, {"a": 3.0})
        assert max_age([r]) == max_age([r])
        assert oldest_staleness([r]) is oldest_staleness([r])


class TestDefaultModeEdges:
    def test_skip_path_no_age_check_beyond_max_stale(self, clock):
        """§3.2 rule 2 / §6.1(15): min_interval > max_stale serves the cache
        with no age check; band FRESH, age_seconds exact."""
        sensor, _ = make_jev_sensor(clock, min_interval=60.0, max_stale=30.0)
        sensor.sense()
        clock[0] = 1045.0  # interval not elapsed -> skip path
        values = sensor.sense()
        assert values == {"danger": 0.5, "threat": "hunting", "hunger": 1.0}
        report = sensor.last_report()
        assert all(m.staleness is Staleness.FRESH for m in report.meta.values())
        assert report.meta["danger"].age_seconds == pytest.approx(45.0)

    def test_judgment_sensor_skip_path_no_age_check(self, clock):
        sensor, _ = make_judgment_sensor(min_interval=60.0, max_stale=30.0)
        sensor.sense()
        clock[0] = 1045.0
        assert sensor.sense() == {"a": 0.5, "b": 0.5}
        report = sensor.last_report()
        assert all(m.staleness is Staleness.FRESH for m in report.meta.values())
        assert report.meta["a"].age_seconds == pytest.approx(45.0)

    def test_non_typesafe_error_propagates_without_telemetry(self, clock):
        records = []
        sensor, _ = make_jev_sensor(clock, telemetry=records.append)
        sensor._client = FakeTypeSafeClient(
            responder=lambda state, questions: (_ for _ in ()).throw(
                RuntimeError("not a TypeSafeError")
            )
        )
        with pytest.raises(RuntimeError, match="not a TypeSafeError"):
            sensor.sense()
        assert records == []
        assert sensor.stats().calls == 0

    def test_dead_retention_worldstate_keeps_last_value(self, clock):
        inner = ScriptedSensor({"a": 1})
        sensor = CachingSensor(
            inner, policy=CachePolicy(stale_after=5.0, max_stale=30.0)
        )
        manager = SensorManager([sensor])
        state = WorldState()
        manager.update_state(state)
        assert state.a == 1
        inner.failure = RuntimeError("boom")
        clock[0] = 1040.0
        with pytest.raises(RuntimeError, match="boom"):
            manager.update_state(state)  # "a" DEAD: fail-loud, nothing written
        assert state.a == 1  # retained, not unset
        assert sensor.health().dead_keys == ("a",)

    def test_jev_dead_key_worldstate_retention(self, clock):
        sensor, _ = make_jev_sensor(clock, stale_after=5.0, max_stale=30.0)
        manager = SensorManager([sensor])
        state = WorldState()
        manager.update_state(state)
        assert state.danger == 0.5
        sensor._client = FakeTypeSafeClient(
            responder=lambda state, questions: (_ for _ in ()).throw(
                TypeSafeError("boom")
            )
        )
        clock[0] = 1040.0
        manager.update_state(state)
        assert state.danger == 0.5  # retained, not unset
