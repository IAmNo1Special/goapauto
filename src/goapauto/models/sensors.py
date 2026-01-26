from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from goapauto.models.worldstate import WorldState


class Sensor(ABC):
    """Base class for environment sensors.

    Sensors are used to perceive the environment and update the
    WorldState with current data.
    """

    @abstractmethod
    def sense(self) -> Dict[str, Any]:
        """Perceive the environment.

        Returns:
            A dictionary of state updates
        """
        pass


class SensorManager:
    """Manages a collection of sensors and updates WorldState."""

    def __init__(self, sensors: Optional[List[Sensor]] = None) -> None:
        self.sensors = sensors or []

    def add_sensor(self, sensor: Sensor) -> None:
        """Add a sensor to the manager."""
        self.sensors.append(sensor)

    def update_state(self, state: WorldState) -> WorldState:
        """Update the given state with data from all sensors.

        Args:
            state: The current world state

        Returns:
            The updated world state (modified in-place or new copy depending on implementation)
        """
        updates = {}
        for sensor in self.sensors:
            try:
                updates.update(sensor.sense())
            except Exception as e:
                # Log error but continue with other sensors
                import logging

                logging.getLogger(__name__).error("Sensor error: %s", e)

        # Update the state with all sensed data
        for key, value in updates.items():
            setattr(state, key, value)

        return state
