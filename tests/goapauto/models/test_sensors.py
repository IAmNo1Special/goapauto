import pytest

from goapauto.models.sensors import Sensor, SensorManager
from goapauto.models.worldstate import WorldState


class ConcreteSensor(Sensor):
    def sense(self):
        return {"sensed_value": 42}


class TestSensors:
    def test_default_constructor(self):
        """Test SensorManager default constructor with no sensors."""
        manager = SensorManager()
        assert manager.sensors == []
        assert manager.update_state(WorldState()) is not None

    def test_add_sensor(self):
        """Test adding a sensor after construction."""
        manager = SensorManager()
        sensor = ConcreteSensor()
        manager.add_sensor(sensor)
        assert manager.sensors == [sensor]

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

    def test_sensor_error_propagates(self, mocker):
        """Test that a failing sensor raises instead of being skipped."""
        bad_sensor = mocker.Mock(spec=Sensor)
        bad_sensor.sense.side_effect = Exception("Boom")

        good_sensor = mocker.Mock(spec=Sensor)
        good_sensor.sense.return_value = {"ok": True}

        manager = SensorManager(sensors=[bad_sensor, good_sensor])
        state = WorldState()

        with pytest.raises(Exception, match="Boom"):
            manager.update_state(state)


class TestSensorManagerInit:
    def test_init_does_not_mutate_sensors_list(self, sensor_mock):
        """SensorManager copies the sensors list; add_sensor won't touch the caller's."""
        sensors = [sensor_mock]
        manager = SensorManager(sensors=sensors)
        manager.add_sensor(sensor_mock)
        assert len(sensors) == 1
        assert len(manager.sensors) == 2
