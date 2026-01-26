import pytest

from goapauto.models.sensors import Sensor, SensorManager
from goapauto.models.worldstate import WorldState


class ConcreteSensor(Sensor):
    def sense(self):
        return {"sensed_value": 42}


class TestSensors:
    def test_sensor_integration(self):
        """Test that data flows from sensor to state."""
        sensor = ConcreteSensor()
        state = WorldState(sensed_value=0)

        manager = SensorManager(sensors=[sensor])
        manager.update_state(state)

        assert state.sensed_value == 42

    def test_multiple_sensors(self, mocker):
        """Test aggregation from multiple sensors."""
        s1 = mocker.Mock(spec=Sensor)
        s1.sense.return_value = {"a": 1}

        s2 = mocker.Mock(spec=Sensor)
        s2.sense.return_value = {"b": 2}

        manager = SensorManager(sensors=[s1, s2])
        state = WorldState()

        manager.update_state(state)
        assert state.a == 1
        assert state.b == 2

    def test_sensor_error_handling(self, mocker, caplog):
        """Test that one sensor failing doesn't stop others."""
        bad_sensor = mocker.Mock(spec=Sensor)
        bad_sensor.sense.side_effect = Exception("Boom")

        good_sensor = mocker.Mock(spec=Sensor)
        good_sensor.sense.return_value = {"ok": True}

        manager = SensorManager(sensors=[bad_sensor, good_sensor])
        state = WorldState()

        manager.update_state(state)

        # Good data should still be present
        assert state.ok == True
        # Error should be logged
        assert "Sensor error" in caplog.text
