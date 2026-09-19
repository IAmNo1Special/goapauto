from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from goapauto.models.worldstate import WorldState


class Sensor(ABC):
    """Base class for environment sensors.

    Sensors are used to perceive the environment and update the
    WorldState with current data.
    """

    @abstractmethod
    def sense(self) -> dict[str, Any]:
        """Perceive the environment.

        Returns:
            A dictionary of state updates
        """
        pass


class SensorManager:
    """Manages a collection of sensors and updates WorldState.

    Thread safety: not thread-safe. Use one SensorManager per thread; do not
    share it across threads without external synchronization.
    """

    def __init__(self, sensors: list[Sensor] | None = None) -> None:
        self.sensors = list(sensors) if sensors is not None else []

    def add_sensor(self, sensor: Sensor) -> None:
        """Add a sensor to the manager."""
        self.sensors.append(sensor)

    def update_state(self, state: WorldState) -> WorldState:
        """Update the given state with data from all sensors.

        Sensor errors propagate to the caller -- a broken sensor fails
        loudly instead of being silently skipped.

        Args:
            state: The current world state

        Returns:
            The updated world state (modified in-place or new copy depending on implementation)
        """
        updates = {}
        for sensor in self.sensors:
            updates.update(sensor.sense())

        # Update the state with all sensed data
        for key, value in updates.items():
            setattr(state, key, value)

        return state
