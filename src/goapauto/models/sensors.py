from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from goapauto.models.worldstate import WorldState

if TYPE_CHECKING:
    from goapauto.models.caching import SensorHealth, SensorReport


class Sensor(ABC):
    """Base class for environment sensors.

    Sensors are used to perceive the environment and update the
    WorldState with current data.
    """

    # Optional human-readable name; health reports and detailed payloads use
    # this or the class name. Subclasses and wrappers set it per instance.
    name: str | None = None

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

    def update_state_detailed(self, state: WorldState) -> list[SensorReport]:
        """Update state like :meth:`update_state`, also returning per-key reports.

        Sensors implementing ``sense_detailed()`` contribute their real
        cache provenance; plain sensors get a synthesized FRESH report (they
        compute live on every update, so their values are fresh by
        construction). Reports follow the same merge order as
        :meth:`update_state`.
        """
        reports = [self._sense_report(sensor) for sensor in self.sensors]
        for report in reports:
            for key, value in report.values.items():
                setattr(state, key, value)
        return reports

    def health(self) -> list[SensorHealth]:
        """Health of every sensor; never raises.

        Sensors implementing ``health()`` report their own bands; plain
        sensors are always FRESH. A sensor whose ``health()`` itself raises
        is reported as dead with the error recorded.
        """
        from goapauto.models.caching import SensorHealth

        entries: list[SensorHealth] = []
        for sensor in self.sensors:
            name = sensor.name or type(sensor).__name__
            health_of = getattr(sensor, "health", None)
            try:
                if callable(health_of):
                    entries.append(health_of())
                else:
                    entries.append(
                        SensorHealth(
                            name=name,
                            status="fresh",
                            last_success_age=None,
                            stale_keys=(),
                            dead_keys=(),
                            error=None,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - health must not raise
                entries.append(
                    SensorHealth(
                        name=name,
                        status="dead",
                        last_success_age=None,
                        stale_keys=(),
                        dead_keys=(),
                        error=f"health_failed:{type(exc).__name__}",
                    )
                )
        return entries

    async def aupdate_state(self, state: WorldState) -> WorldState:
        """Async :meth:`update_state`.

        Sensors implementing ``asense()`` are awaited; the rest are read via
        their sync ``sense()`` inline -- never ``to_thread`` (design §7).
        """
        updates: dict[str, Any] = {}
        for sensor in self.sensors:
            updates.update(await _asense_values(sensor))
        for key, value in updates.items():
            setattr(state, key, value)
        return state

    async def aupdate_state_detailed(self, state: WorldState) -> list[SensorReport]:
        """Async :meth:`update_state_detailed`; same per-sensor preference."""
        reports = [await _asense_report(sensor) for sensor in self.sensors]
        for report in reports:
            for key, value in report.values.items():
                setattr(state, key, value)
        return reports

    @staticmethod
    def _sense_report(sensor: Sensor) -> SensorReport:
        sense_detailed = getattr(sensor, "sense_detailed", None)
        if callable(sense_detailed):
            return sense_detailed()
        return _live_report(sensor, sensor.sense())


def _sensor_name(sensor: Sensor) -> str:
    return sensor.name or type(sensor).__name__


def _live_report(sensor: Sensor, values: dict[str, Any]) -> SensorReport:
    """Synthesize a SensorReport for a sensor with no cache clock.

    Plain sensors compute live on every update, so their values are FRESH
    at build time by construction.
    """
    from goapauto.models.caching import SensorReport, Staleness, ValueMeta

    name = _sensor_name(sensor)
    now = time.monotonic()
    return SensorReport(
        values=dict(values),
        meta={
            key: ValueMeta(
                age_seconds=0.0,
                staleness=Staleness.FRESH,
                source=name,
                as_of_monotonic=now,
            )
            for key in values
        },
        sensor=name,
    )


async def _asense_values(sensor: Sensor) -> dict[str, Any]:
    """Await ``asense()`` when present, else call sync ``sense()`` inline."""
    asense = getattr(sensor, "asense", None)
    if callable(asense):
        return await asense()
    if callable(getattr(sensor, "sense", None)):
        return sensor.sense()
    raise TypeError(
        f"Sensor {type(sensor).__name__!r} implements neither asense() nor sense()."
    )


async def _asense_report(sensor: Sensor) -> SensorReport:
    """Prefer ``asense_detailed()``, then ``asense()``, then sync reads."""
    asense_detailed = getattr(sensor, "asense_detailed", None)
    if callable(asense_detailed):
        return await asense_detailed()
    asense = getattr(sensor, "asense", None)
    if callable(asense):
        return _live_report(sensor, await asense())
    sense_detailed = getattr(sensor, "sense_detailed", None)
    if callable(sense_detailed):
        return sense_detailed()
    if callable(getattr(sensor, "sense", None)):
        return _live_report(sensor, sensor.sense())
    raise TypeError(
        f"Sensor {type(sensor).__name__!r} implements neither asense() nor sense()."
    )
