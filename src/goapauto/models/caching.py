"""Sensor caching and stale-data behavior (design 02).

Every sensor read is tagged with its age and a freshness band, so the
planner and the strategies downstream can tell live readings from cached
ones and stop trusting data that is past its deadline.

The model has three pieces:

* :class:`Staleness` -- the FRESH/STALE/DEAD ladder (design §3.1).
* :class:`CachePolicy` -- the per-sensor TTL configuration (design §3.2).
* :class:`SensorReport` / :class:`SensorHealth` -- the per-read and
  per-sensor introspection payloads (design §5).

:class:`CachingSensor` wraps any plain :class:`~goapauto.models.sensors.Sensor`
with a per-key TTL cache. :class:`~goapauto.models.jev.JevSensor` and
:class:`~goapauto.models.judgment.JudgmentSensor` are built on the same
shared per-key engine (:class:`_PerKeyCache`) rather than re-implementing
the ladder -- one band computation, one write rule, no drift.

Composition rule (design §4): staleness composes by conjunction, not
multiplication. A STALE reading never inflates a confidence; it gates it.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from goapauto.models.sensors import Sensor

__all__ = [
    "AsyncSensor",
    "CachePolicy",
    "CachingSensor",
    "SensorHealth",
    "SensorReport",
    "Staleness",
    "StalePolicy",
    "ValueMeta",
    "max_age",
    "oldest_staleness",
]


class Staleness(Enum):
    """Freshness band of one cached reading (design §3.1)."""

    FRESH = "fresh"
    STALE = "stale"
    DEAD = "dead"


class StalePolicy(Enum):
    """What a sensor does with STALE readings (design §3.2)."""

    KEEP_FLAGGED = "keep_flagged"
    DROP = "drop"


@dataclass(frozen=True)
class CachePolicy:
    """Per-sensor TTL configuration (design §3.2).

    ``stale_after=None`` selects the legacy cliff: no STALE band, readings
    are FRESH until ``max_stale`` and DEAD after. Both built-in judgment
    sensors keep this default so existing deployments see zero behavior
    change; :class:`CachingSensor` users opt into the ladder explicitly.
    """

    stale_after: float | None = None
    max_stale: float = 30.0
    stale_policy: StalePolicy = StalePolicy.KEEP_FLAGGED

    def __post_init__(self) -> None:
        if self.max_stale < 0:
            raise ValueError("max_stale must be non-negative.")
        if self.stale_after is not None and not (
            0 <= self.stale_after <= self.max_stale
        ):
            raise ValueError("stale_after must satisfy 0 <= stale_after <= max_stale.")


@dataclass(frozen=True)
class ValueMeta:
    """Provenance of one value in a :class:`SensorReport` (design §5)."""

    age_seconds: float
    staleness: Staleness
    source: str  # sensor name on live keys, "cache" on replayed ones
    as_of_monotonic: float


@dataclass(frozen=True)
class SensorReport:
    """One sensor update with per-key provenance (design §5)."""

    values: dict[str, Any]
    meta: dict[str, ValueMeta]
    sensor: str


@dataclass(frozen=True)
class SensorHealth:
    """Point-in-time health of one sensor (design §6).

    Never raises out of ``health()``; a sensor that cannot even report its
    health surfaces ``status="dead"`` with ``error`` set instead.
    """

    name: str
    status: str  # "fresh" | "stale" | "dead": worst band across keys
    last_success_age: float | None
    stale_keys: tuple[str, ...]
    dead_keys: tuple[str, ...]
    error: str | None


@runtime_checkable
class AsyncSensor(Protocol):
    """Structural type for sensors whose read is a coroutine."""

    name: str | None
    ...

    async def asense(self) -> dict[str, Any]: ...
    async def asense_detailed(self) -> SensorReport: ...


_RANK = {Staleness.FRESH: 0, Staleness.STALE: 1, Staleness.DEAD: 2}


def _contains_nan(observation: Mapping[str, Any]) -> bool:
    """Check for top-level NaN floats.

    NaN is unordered: it never compares equal, so an observation containing
    NaN can never be proven unchanged. Change detection treats it as
    always-changed (documented NaN caveat). Shallow by design; nested NaN
    is the caller's responsibility.
    """
    return any(isinstance(v, float) and math.isnan(v) for v in observation.values())


class _PerKeyCache:
    """Shared per-key (value, as_of_monotonic) store and band engine.

    One implementation serves :class:`CachingSensor`,
    :class:`~goapauto.models.jev.JevSensor` and
    :class:`~goapauto.models.judgment.JudgmentSensor` so the §3.1 ladder
    and the §3.3 write rule cannot drift between sensors.

    Write rule (§3.3): a successful read refreshes the keys it reports and
    leaves every other key untouched. A successful read never deletes --
    keys disappear only via the caller's own removal, never via staleness.
    """

    def __init__(self, policy: CachePolicy) -> None:
        self._policy = policy
        self._entries: dict[str, tuple[Any, float]] = {}
        # §3.2 rule 3: the shared success instant. Advances on every
        # successful backend read, even one that reports no keys (e.g. 04
        # fully-gated): the legacy whole-cache stale check keys off last
        # success, not last key write.
        self._last_success: float | None = None

    def write(self, updates: Mapping[str, Any], as_of: float) -> None:
        """Refresh the keys present in ``updates``; absent keys are kept."""
        for key, value in updates.items():
            self._entries[key] = (value, as_of)
        self._last_success = as_of

    def write_new(self, updates: Mapping[str, Any], as_of: float) -> None:
        """Write only keys not already cached (unchanged-output path)."""
        for key, value in updates.items():
            if key not in self._entries:
                self._entries[key] = (value, as_of)
        self._last_success = as_of

    def band(self, as_of: float, now: float) -> Staleness:
        """The §3.1 ladder for one cached instant."""
        age = now - as_of
        stale_after = self._policy.stale_after
        if stale_after is None:
            # Legacy cliff: no STALE band; FRESH until max_stale, then DEAD.
            return Staleness.FRESH if age <= self._policy.max_stale else Staleness.DEAD
        if age <= stale_after:
            return Staleness.FRESH
        if age <= self._policy.max_stale:
            return Staleness.STALE
        return Staleness.DEAD

    def serve(
        self,
        now: float,
        *,
        live_keys: Collection[str],
        source: str,
        skip_age_check: bool = False,
        only_live_keys: bool = False,
    ) -> tuple[dict[str, Any], dict[str, ValueMeta]]:
        """Values servable right now, with per-key metadata.

        ``skip_age_check`` reproduces the 0.5.0 skip path: every cached key
        is served as FRESH with no age check (default mode only).
        ``only_live_keys`` (success path): report only the keys this read
        produced. Absent/gated keys keep their cached value and age but are
        omitted here so they do not re-enter WorldState as fresh.
        """
        values: dict[str, Any] = {}
        meta: dict[str, ValueMeta] = {}
        if only_live_keys:
            candidate_keys = [k for k in live_keys if k in self._entries]
        else:
            candidate_keys = list(self._entries.keys())
        for key in candidate_keys:
            value, as_of = self._entries[key]
            if skip_age_check:
                band = Staleness.FRESH
            else:
                band = self.band(as_of, now)
                if band is Staleness.DEAD:
                    continue
                if (
                    band is Staleness.STALE
                    and self._policy.stale_policy is StalePolicy.DROP
                ):
                    continue
            values[key] = value
            meta[key] = ValueMeta(
                age_seconds=now - as_of,
                staleness=band,
                source=source if key in live_keys else "cache",
                as_of_monotonic=as_of,
            )
        return values, meta

    def key_bands(self, now: float) -> list[tuple[str, Staleness]]:
        """Current band of every cached key (health/diagnostics views)."""
        return [
            (key, self.band(as_of, now)) for key, (_, as_of) in self._entries.items()
        ]

    def last_success_instant(self) -> float | None:
        """Shared success instant, or None before the first successful read."""
        return self._last_success

    def health(self, name: str, last_error: str | None, now: float) -> SensorHealth:
        """Build the design §6 health view over the current bands."""
        stale_keys: list[str] = []
        dead_keys: list[str] = []
        worst = Staleness.FRESH
        for key, band in self.key_bands(now):
            if band is Staleness.STALE:
                stale_keys.append(key)
            elif band is Staleness.DEAD:
                dead_keys.append(key)
            if _RANK[band] > _RANK[worst]:
                worst = band
        last_success = self.last_success_instant()
        return SensorHealth(
            name=name,
            status=worst.value,
            last_success_age=(None if last_success is None else now - last_success),
            stale_keys=tuple(stale_keys),
            dead_keys=tuple(dead_keys),
            error=last_error,
        )

    def diagnostics(self, report: SensorReport | None, now: float) -> dict[str, Any]:
        """Build the exact design §6 diagnostics payload (never raises)."""
        try:
            if report is None:
                return {}
            dead = [key for key, band in self.key_bands(now) if band is Staleness.DEAD]
            bands = [entry.staleness for entry in report.meta.values()]
            if dead:
                bands.append(Staleness.DEAD)
            worst = Staleness.FRESH
            for band in bands:
                if _RANK[band] > _RANK[worst]:
                    worst = band
            return {
                "data_age_seconds": {
                    key: entry.age_seconds for key, entry in report.meta.items()
                },
                "staleness": {
                    key: entry.staleness.value for key, entry in report.meta.items()
                },
                "dead_keys": dead,
                "worst_staleness": worst.value,
            }
        except Exception:
            return {}


class CachingSensor(Sensor):
    """Wrap any sensor with a per-key TTL cache and a staleness ladder.

    On each :meth:`sense`, the inner sensor is read; when it succeeds its
    output is merged into the cache and the merged view is served with
    per-key age metadata. When it raises, the failure is absorbed only if
    some cached keys are still servable -- otherwise the original
    exception propagates (design §4: fail loud, never silently wrong).

    An optional ``on_change`` hook fingerprints the inner output; when the
    fingerprint is unchanged the backend cost is skipped and the cached
    view is served, with genuinely new keys still admitted as FRESH.
    The hook must be pure: its return value is compared with ``==`` and a
    raising hook propagates.

    Not thread-safe: one instance per thread, like every other sensor.
    """

    def __init__(
        self,
        inner: Sensor,
        policy: CachePolicy | None = None,
        name: str | None = None,
        on_change: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._inner = inner
        self._policy = policy if policy is not None else CachePolicy()
        self._cache = _PerKeyCache(self._policy)
        self._on_change = on_change
        self._fingerprint: Any = None
        self._has_fingerprint = False
        self._last_error: str | None = None
        self._last_report: SensorReport | None = None
        self.name = name

    def _display_name(self) -> str:
        return self.name or type(self._inner).__name__

    def sense(self) -> dict[str, Any]:
        """Merged cached view; raises the inner error when nothing is usable."""
        return self._sense_report().values

    def sense_detailed(self) -> SensorReport:
        """Merged cached view with per-key age and staleness metadata."""
        return self._sense_report()

    def last_report(self) -> SensorReport | None:
        """Most recent report, or None before the first successful read."""
        return self._last_report

    def health(self) -> SensorHealth:
        """Current band/health view; never raises."""
        return self._cache.health(
            self._display_name(), self._last_error, time.monotonic()
        )

    def diagnostics(self) -> dict[str, Any]:
        """Exact design §6 payload; ``{}`` before the first report."""
        return self._cache.diagnostics(self._last_report, time.monotonic())

    def _sense_report(self) -> SensorReport:
        try:
            updates = self._inner.sense()
        except Exception as exc:
            report = self._serve_on_error(exc)
            if report is None:
                raise
            self._last_report = report
            return report
        now = time.monotonic()
        if self._on_change is not None:
            fingerprint = self._on_change(updates)
            if self._has_fingerprint and fingerprint == self._fingerprint:
                self._cache.write_new(updates, now)
                report = self._build_report(now, live_keys=updates.keys())
                self._last_report = report
                return report
            self._fingerprint = fingerprint
            self._has_fingerprint = True
        self._cache.write(updates, now)
        self._last_error = None
        report = self._build_report(now, live_keys=updates.keys())
        self._last_report = report
        return report

    def _serve_on_error(self, exc: Exception) -> SensorReport | None:
        """Absorb ``exc`` when cached keys are still servable; else None."""
        self._last_error = type(exc).__name__
        now = time.monotonic()
        values, meta = self._cache.serve(now, live_keys=(), source=self._display_name())
        if not values:
            return None
        return SensorReport(values=values, meta=meta, sensor=self._display_name())

    def _build_report(self, now: float, *, live_keys: Collection[str]) -> SensorReport:
        values, meta = self._cache.serve(
            now, live_keys=live_keys, source=self._display_name()
        )
        return SensorReport(values=values, meta=meta, sensor=self._display_name())


def max_age(reports: list[SensorReport]) -> float | None:
    """Oldest value age across reports; None when no values are present."""
    oldest: float | None = None
    for report in reports:
        for entry in report.meta.values():
            if oldest is None or entry.age_seconds > oldest:
                oldest = entry.age_seconds
    return oldest


def oldest_staleness(reports: list[SensorReport]) -> Staleness:
    """Worst staleness band across reports; FRESH when empty."""
    worst = Staleness.FRESH
    for report in reports:
        for entry in report.meta.values():
            if _RANK[entry.staleness] > _RANK[worst]:
                worst = entry.staleness
    return worst
