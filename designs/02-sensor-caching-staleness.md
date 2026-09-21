# Design 02 — Sensor caching & stale-data behavior

**Status:** implemented (Phase 2, 0.6.0). `caching.py`, `CachingSensor`, opt-in `stale_after`/`max_stale`.
**Revision:** 2 — 2026-09-21. Addresses the red-team review
(`reviews/02-sensor-caching-staleness-review.md`, all 14 attacks) and
implements the coordinator's binding rulings: **A** (conjunction, not
multiplicative composition), **E** (every emitted type has a named
consumer), **G** (no sanctioned cross-thread `judge()` pattern), and the
DEAD-keys retention decision.
**Builds on:** v0.5.0 `JevSensor` resilience contracts (`min_interval`,
`resense_on_change`, `max_stale`, `JevCallRecord`, `JevStats`) — this design
extends them, never redefines them.
**Related:** Design 01 (runtime/replan budgets), 03 (action interruption),
04 (confidence gating), 05 (provider-independent judgment interface),
06 (why-diagnostics).

---

## 1. Goal and non-goals

### Goal

Replace 0.5.0's binary staleness cliff (fresh → dead at `max_stale`, with
`{}` returned past the cliff) with a **degradation ladder**
fresh → stale-but-flagged → dead, where:

- staleness is tracked **per key**, not per whole cache, so one slow or
  failed question does not poison the rest;
- staleness **metadata** (age, band, source) travels on a channel
  that never silently pollutes WorldState keys;
- the machinery works for **plain `Sensor` subclasses**, not just
  `JevSensor`;
- `SensorManager` can coordinate per-sensor policies and report an
  **aggregate health view** (which sensors are fresh / stale / dead);
- sense-on-change uses a **tunable notion of "changed"**, not just `==`.

### Non-goals (explicitly NOT built, with reasons)

1. **Background/prefetch refresh threads, and no sanctioned workaround
   for them. OUT OF SCOPE.** The library is explicitly not thread-safe
   (`SensorManager`, `JevSensor`, strategies all document per-thread
   ownership). A background refresher would need locks around the cache,
   the WorldState, and the planner loop — real complexity for a library
   whose contract says "one instance per thread." There is **no supported
   prefetch story in Phase 1**: hosts must not call `sense()`/`judge()`
   from multiple threads on one instance, and the previous revision's
   suggestion of host-side `judge()` on a refresh thread is retracted —
   it races on `_cached`/`_last_success` against the tick thread with no
   happens-before edge. The async seam is `SensorManager.aupdate_state()`
   (§2.6): synchronous ownership, no shared mutation. The honest
   consequence, stated plainly: a 292ms p50 judgment (measured, Stage B)
   on a 16ms tick still costs ~18 ticks inline when a re-judge fires.
   `min_interval` bounds *how often* that happens; the ladder makes the
   resulting staleness *visible*. Phase 1 has no mechanism that hides the
   latency — hiding it is 05's future `AsyncJudge` territory.
2. **Rewriting the `Sensor.sense() -> dict` contract.** `sense()` keeps its
   signature and dict return. All new information rides a parallel
   detailed channel (`sense_detailed()` / `last_report()` /
   `diagnostics()`). Callers that ignore staleness keep working
   byte-for-byte as in 0.5.0.
3. **A planner that auto-reacts to staleness.** The planner stays pure: it
   plans over the values in the WorldState. Whether stale values should
   trigger replans, gate actions, or compose with confidence is the job of
   the budgets / interruption / confidence-gating designs. We expose the
   facts (age, band); they own the policy. A "smart" cache that silently
   drops keys the planner depends on would be a second, hidden planner —
   which is also why the manager never unsets DEAD keys (§3.8 edge 1,
   decided).
4. **Persistence of the cache across process restarts.** Caches are
   in-memory, monotonic-clock-aged, and die with the instance. Disk
   persistence would need clock-domain and schema decisions that belong to
   the host, not the library.
5. **Changing 0.5.0 defaults.** `JevSensor` with no new arguments behaves
   exactly as 0.5.0: `stale_after` defaults to `None`, which reproduces the
   cliff at `max_stale`. The ladder is opt-in per sensor. The equivalence
   is specified as a binding contract in §3.2 and pinned by a differential
   test harness in §6.1 — the legacy suite alone is not the proof.

---

## 2. User-side API

### 2.1 New core types (`goapauto/models/caching.py`, suggested)

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class Staleness(Enum):
    FRESH = "fresh"   # younger than stale_after (or a live re-sense)
    STALE = "stale"   # older than stale_after, younger than max_stale
    DEAD = "dead"     # older than max_stale: key omitted, as in 0.5.0


@dataclass(frozen=True)
class ValueMeta:
    """Per-key freshness metadata. Travels beside the value, never inside it.

    ``age_seconds`` is computed at report-build time; for decisions,
    recompute ``now - as_of_monotonic``. Transition detection must use
    (``staleness``, ``as_of_monotonic``), never ``age_seconds``.
    """

    age_seconds: float  # time.monotonic() - as_of_monotonic, at build time
    staleness: Staleness
    source: str  # sensor name when live-sensed this call, else "cache"
    as_of_monotonic: float  # monotonic timestamp of the underlying read


@dataclass(frozen=True)
class SensorReport:
    """What sense_detailed() returns: values plus their freshness story."""

    values: dict[str, Any]  # exactly what sense() returned
    meta: dict[str, ValueMeta]  # key -> metadata; set(meta) == set(values)
    sensor: str  # name or type(sensor).__name__ (see §2.4)


class StalePolicy(Enum):
    KEEP_FLAGGED = "keep_flagged"  # serve STALE values with their flag
    DROP = "drop"  # at stale_after, behave like 0.5.0's cliff


@dataclass(frozen=True)
class CachePolicy:
    """How long a cached value may live. Zero/None disables caching."""

    stale_after: float | None = None  # None -> 0.5.0 cliff at max_stale
    max_stale: float = 30.0  # unchanged 0.5.0 semantics: past this, DEAD
    stale_policy: StalePolicy = StalePolicy.KEEP_FLAGGED

    def __post_init__(self) -> None:
        if self.max_stale < 0:
            raise ValueError("max_stale must be non-negative.")
        if self.stale_after is not None and not (
            0 <= self.stale_after <= self.max_stale
        ):
            raise ValueError("stale_after must satisfy 0 <= stale_after <= max_stale.")
```

Per-key TTL *overrides* are deliberately absent: per-key *aging*
(timestamps per key) already delivers the "one slow question doesn't
poison the rest" win, which is the goal's actual ask. Per-key policy
adds a validation surface and a typo-silent hazard (an override naming a
key that never arrives does nothing, loudly failing nowhere) with no
named Phase-1 consumer. Ship per-sensor policy plus per-key aging; add
overrides when a real caller needs them.

### 2.2 `CachingSensor` — the generic wrapper (works for any `Sensor`)

```python
class CachingSensor(Sensor):
    """Wrap any Sensor with a per-key TTL cache and a staleness ladder.

    Delegates to ``inner.sense()``; caches the returned dict per key with a
    monotonic timestamp. On ``inner.sense()`` raising, serves cached values
    per the CachePolicy ladder instead of propagating — EXCEPT that the
    wrapper never swallows errors when there is no usable cache, in which
    case the original exception propagates (fail-loud, like SensorManager).
    """

    def __init__(
        self,
        inner: Sensor,
        policy: CachePolicy | None = None,
        name: str | None = None,
        on_change: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None: ...

    def sense(self) -> dict[str, Any]:
        """dict of live-or-cached values. STALE values ARE included (flagged
        in the report); DEAD values are omitted."""

    def sense_detailed(self) -> SensorReport: ...
    def last_report(self) -> SensorReport | None: ...
    def health(self) -> SensorHealth: ...
    def diagnostics(self) -> dict[str, Any]: ...
```

`on_change` is the semantic-diff hook for sense-on-change (see §3.5); it
fingerprints the raw `inner.sense()` output to decide whether anything
actually changed.

### 2.3 `JevSensor` extensions (additive, defaults unchanged)

```python
JevSensor(
    ...,
    stale_after: float | None = None,  # NEW; None -> 0.5.0 cliff behavior
    stale_policy: StalePolicy = StalePolicy.KEEP_FLAGGED,  # NEW
    name: str | None = None,  # NEW; for health reports / traces
    observation_fingerprint: Callable[[dict[str, Any]], Any] | None = None,  # NEW
)
```

New methods: `sense_detailed() -> SensorReport`, `last_report()`,
`health() -> SensorHealth`, `diagnostics() -> dict[str, Any]`.
`sense()` / `judge()` / `stats()` / `close()` / telemetry semantics are
unchanged when the new arguments are not passed — §3.2 specifies exactly
what "unchanged" means.

`JevSensor` validates its own `stale_after`/`max_stale` with the same
rule as `CachePolicy.__post_init__`. No new fields are added to
`JevCallRecord` by this design: record evolution is owned by design 04,
which touches it more deeply; 02's band information travels via
`SensorReport` / `health()` / `diagnostics()` instead (see §9, attack 4).

### 2.4 Naming (`Sensor.name`)

`Sensor` gains an optional, settable `name: str | None` attribute
(default `None`; subclass or wrapper sets it). Health reports and trace
payloads use `name or type(sensor).__name__` — for `CachingSensor`,
`name or type(inner).__name__`, since the staleness story is about the
data source. This is additive: existing subclasses are unaffected.

### 2.5 `SensorManager` coordination

```python
@dataclass(frozen=True)
class SensorHealth:
    name: str
    status: str  # "fresh" | "stale" | "dead": worst band across the keys
    last_success_age: float | None  # seconds since last successful re-sense
    stale_keys: tuple[str, ...]  # cache insertion order
    dead_keys: tuple[str, ...]  # cache insertion order
    error: str | None  # exception class name from the most recent failed
    # sense; None if the last sense succeeded or none has run


class SensorManager:
    ...
    def update_state_detailed(self, state: WorldState) -> list[SensorReport]:
        """Like update_state, but returns one SensorReport per sensor."""

    def health(self) -> list[SensorHealth]:
        """Aggregate health, in sensor order. Never raises: a sensor whose
        own health() raises contributes a worst-case entry (status="dead",
        error="health_failed:<ExcClass>") — unobservable is treated as
        unusable, the fail-safe direction."""

    async def aupdate_state(self, state: WorldState) -> WorldState: ...
    async def aupdate_state_detailed(
        self, state: WorldState
    ) -> list[SensorReport]: ...
```

- `update_state` is untouched: it merges dicts exactly as today (fail-loud
  on sensor error). `update_state_detailed` merges dicts the same way but
  also collects per-sensor reports.
- `SensorHealth.status` is the plain string vocabulary `"fresh" |
  "stale" | "dead"` (the `Staleness` values), so the aggregate view is
  JSON-safe without conversion. `ValueMeta.staleness` keeps the
  `Staleness` enum; its `.value` is the same vocabulary.
- Sensors without cache support (plain `Sensor` subclasses) report
  `status="fresh"`, `last_success_age=None`, empty key tuples: their
  values are computed live on each update, so "fresh" means "no cache
  clock exists," not a claim about data age. No `known` flag — status
  plus nullable fields is the whole story.

### 2.6 Async sensing (slim seam, no thread-hopping)

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class AsyncSensor(Protocol):
    name: str | None
    async def asense(self) -> dict[str, Any]: ...
    async def asense_detailed(self) -> SensorReport: ...
```

- `SensorManager.aupdate_state[_detailed]` awaits `asense()` where the
  sensor provides it and calls `sense()` **inline** otherwise — no thread
  hop is involved in an inline sync call, so the thread-safety contract
  is not violated. `TypeError` is reserved for sensors implementing
  neither method.
- There is no `to_async` helper: wrapping `sense()` in
  `asyncio.to_thread` reintroduces the cross-thread sharing this design
  refuses to bless. Hosts that want the hop write it themselves, with the
  caveat visible at their own call site.
- Phase 1 has no in-tree producer (05 defers `AsyncJudge`); the seam
  exists for host-owned async sensors.

### 2.7 Aggregation helpers and the diagnostics mapping

```python
def max_age(reports: list[SensorReport]) -> float | None:
    """Oldest per-key age_seconds across all reports (seconds).

    None when there are no keys. This is the scalar that design 01's
    ``should_replan(sensor_age=...)`` consumes: the conservative answer
    to "how old is the oldest ground truth this plan would be built on."
    """


def oldest_staleness(reports: list[SensorReport]) -> Staleness:
    """Worst band across all reports (dead > stale > fresh).

    FRESH when there are no keys.
    """
```

The scalar cannot express "3 keys FRESH, 1 STALE, 1 DEAD" — that
information lives in `meta`. `max_age` is the conservative, all-or-nothing
aggregation matching 01's all-or-nothing `SENSORS_STALE` gate; hosts that
need per-key policy read `SensorReport.meta` directly.

```python
def diagnostics(self) -> dict[str, Any]:
    """Duck-typed mapping for the diagnostics inspector (design 06).

    JSON-safe, exact keys; 06 merges it into its sensor_update payload.
    {} when no report exists yet. Never raises.
    """
    # {
    #     "data_age_seconds": {key: float, ...},  # per-key, at last report build
    #     "staleness": {key: "fresh" | "stale", ...},  # same key set; DEAD absent
    #     "dead_keys": [key, ...],
    #     "worst_staleness": "fresh" | "stale" | "dead",
    # }
```

This is the named consumer that wires `SensorReport`/`ValueMeta` into
the system (ruling E): 06 consumes `diagnostics()` + `health()`, 01
consumes `max_age()`/`oldest_staleness()`, 03 consumes `last_report()`
transitions. The old 06 sketch keys (`data_age_ms`,
`cache="hit"|"miss"|"stale"`) are superseded by the agreed vocabulary:
states exactly `fresh`/`stale`/`dead`, per-key ages in seconds (float,
monotonic-derived).

### 2.8 Example usage

```python
from goapauto import SensorManager, WorldState
from goapauto.models.caching import CachingSensor, CachePolicy, max_age

policy = CachePolicy(
    stale_after=5.0,  # after 5s without a re-sense, values go STALE-but-flagged
    max_stale=30.0,  # after 30s, DEAD (omitted) — 0.5.0 behavior
)
sensor = CachingSensor(VisionSensor(), policy=policy, name="vision")

mgr = SensorManager([sensor])
state = WorldState()
reports = mgr.update_state_detailed(state)
for rep in reports:
    for key, meta in rep.meta.items():
        if meta.staleness is Staleness.STALE:
            print(f"{key} is {meta.age_seconds:.1f}s old — plan cautiously")

for h in mgr.health():
    print(h.name, h.status, "stale:", h.stale_keys, "dead:", h.dead_keys)

# Design 01's replan policy consumes the scalar:
# should_replan(state, goal, plan, sensor_age=max_age(reports))
```

```python
# JevSensor: opt into the ladder without touching anything else
from goapauto import JevSensor

jev = JevSensor(observe=obs, questions=qs, stale_after=10.0, name="threat-judge")
updates = jev.sense()  # unchanged dict contract
report = jev.sense_detailed()  # values + ValueMeta per key
```

### 2.9 Public-surface delta (`goapauto/__init__.py` `__all__`)

Add exactly: `CachingSensor`, `CachePolicy`, `StalePolicy`, `Staleness`,
`ValueMeta`, `SensorReport`, `SensorHealth`, `AsyncSensor`, `max_age`,
`oldest_staleness`. Cut from the previous revision's sketch list:
`staleness_discount` (ruling A), `tolerant_fingerprint` (cut — hook
only), `to_async` (cut — no blessed thread hop).
---

## 3. Precise semantics

### 3.1 The ladder (per key)

For each cached key with `(stale_after, max_stale)`, let
`age = now_monotonic - as_of`:

| age | band | `sense()` includes the value? | `meta[key]` |
|---|---|---|---|
| `age <= stale_after` | FRESH | yes | `Staleness.FRESH` |
| `stale_after < age <= max_stale` | STALE | yes, iff `stale_policy is KEEP_FLAGGED` | `Staleness.STALE` |
| `age > max_stale` | DEAD | **no** (key omitted, exactly like 0.5.0 today) | key absent from `meta` |

- `stale_after=None` (the default) reproduces 0.5.0 exactly: no STALE
  band; on the failure path, cached values are served while
  `age <= max_stale` and the key is dropped past it. In this mode
  served-from-cache values report `Staleness.FRESH` (the only band 0.5.0
  knew). **Default-mode banding applies only where 0.5.0 applied it —
  the failure path** (see §3.2 rule 2). With `stale_after` set (opt-in),
  bands apply on every path, including the `min_interval` skip path.
- With `stale_after=0.0`: every cache hit is immediately STALE (useful
  for "flag everything not re-sensed this tick" policies).
- `max_stale=0.0` keeps 0.5.0 meaning: no reuse at all; first failure
  drops the keys (and `CachingSensor` propagates the original exception —
  see §4).
- Boundary comparisons use `<=` on the fresh side, matching 0.5.0's
  `time.monotonic() - self._last_success <= self._max_stale`.
- **Validation:** `stale_after` (when not None) must satisfy
  `0 <= stale_after <= max_stale`, else `ValueError` at construction.
  `max_stale < 0` raises as in 0.5.0.
- Up front, the `min_interval` interaction, stated plainly: with the
  default `min_interval=0.0` (and `resense_on_change=True`),
  `_should_resense` re-judges on **every** `sense()` call — unchanged
  observations included (the interval check `(now - last_call) >= 0.0`
  is always true; this is the 0.5.0 quirk, preserved). The cache is
  therefore ~0s old on the success path and the ladder's bands are
  exercised only on the failure path. Setting `min_interval > 0` makes
  the STALE band reachable on the success path **by design**: a sensor
  with `min_interval=60, stale_after=5` will routinely serve STALE data
  between re-queries — that is the operator's explicit choice, and the
  flag makes it visible instead of silent.

### 3.2 Default-mode 0.5.0-equivalence contract (binding)

When `stale_after=None` (the default) and
`observation_fingerprint=None` (the default), the implementation MUST
reproduce 0.5.0's observable behavior exactly. "Observable" means: the
sequence of `sense()` return dicts, `stats()` counters, the
`JevCallRecord` stream delivered to telemetry, the `system_one` call
sequence (arguments and count), and which exceptions propagate. The
contract, derived line-by-line from 0.5.0
`JevSensor._judge`/`_should_resense`:

1. **Re-sense decision is identical.** Same inputs, same comparisons:
   `_last_observation is None` → re-sense; `resense_on_change and
   observation != _last_observation` → re-sense; else
   `(now - _last_call) >= min_interval` → re-sense. The fingerprint hook
   is not consulted when `None`.
2. **Skip path (no re-sense attempted): serve the full cached dict
   as-is.** 0.5.0 performs no age check on this path — neither does
   default mode, even when the cached age exceeds `max_stale` (reachable
   when `min_interval > max_stale`). Served keys report band FRESH;
   `age_seconds` is exact.
3. **Success path: one shared `as_of`.** All keys of the extracted
   output share a single timestamp (the `_last_success` instant); the
   cache is replaced wholesale with `dict` insertion order equal to
   `_mapping` order, as 0.5.0's `_extract`. Under defaults, per-key
   `as_of` values are always coincident, so the per-key generalization
   collapses to 0.5.0's whole-cache arithmetic.
4. **Failure path (`TypeSafeError` only):** `stale = (now -
   last_success) <= max_stale`, where `last_success` is the shared
   success instant — numerically identical to 0.5.0's `_last_success`
   by rule 3. If stale: serve the full cached dict, band FRESH,
   `stale_cache_hits += 1`, telemetry `error=<ClassName>`,
   `stale_cache_hit=True`. If not stale: return `{}`, cache retained
   (0.5.0 does not clear it), telemetry `stale_cache_hit=False`.
5. **Non-`TypeSafeError` exceptions propagate with no telemetry
   record.** 0.5.0 catches only `TypeSafeError`.
6. **Telemetry is field-identical.** `source="sensor"`, `latency_ms =
   (monotonic() - start) * 1000.0`, usage passthrough,
   `error` = exception class name or `None`, `stale_cache_hit` bool.
   This design adds no `JevCallRecord` fields.
7. **`_last_observation` updates only on success.** A failed call does
   not become the change-detection baseline (0.5.0 behavior).

Any implementation sharing code between the ladder and the legacy path
must pass the equivalence harness (§6.1). The legacy test suite staying
green is necessary but not sufficient — it was written against
whole-cache behavior and cannot detect drift it wasn't designed to
catch.

### 3.3 The cache write rule (per-key, binding) and the 04 composition

**Write rule.** On a successful re-judge producing output dict
`updates`:

- every key in `updates` is (re)written: `cache[key] = (value,
  as_of=now)`;
- every cached key **absent** from `updates` keeps its previous
  `(value, as_of)` untouched — it keeps aging toward STALE/DEAD;
- a successful call never deletes a cache entry.

DEAD entries may be pruned from the store (unobservable: a pruned DEAD
key and a retained DEAD key are both omitted from `sense()` and both
refresh identically on the next success). `CachingSensor` follows the
same rule for the wrapped sensor's output dict.

**Composition with confidence gating (ruling A: conjunction).**
Staleness and confidence are independent axes. A judgment is usable iff
it passes **both** gates: confidence ≥ threshold (judged at answer
time) AND age within policy (judged at replay time). There is no
multiplicative `effective_confidence`; the previous revision's
`staleness_discount` helper is cut — its sole purpose was a composition
the gating design refused, and age is not a discount on a judgment the
model already made.

**Joint contract with design 04.** 04's gating is a *per-key write
filter* on this rule: a gated question's answer is treated exactly like
"key absent from this call's output" — its cache entry is **not
refreshed and not deleted**; its `as_of` keeps aging honestly toward
STALE/DEAD. Consequences:

- a previously passing judgment survives a later low-confidence blip
  in the cache, aging truthfully;
- the failure path replays only judgments that passed the gate when
  cached — a stale cached judgment that "would also gate today" cannot
  exist, because gating happens at write and replay happens at read;
- an all-gated call with a warm cache serves the aging cached values
  per the ladder (not `{}`); an all-gated call with an empty cache
  returns `{}`. The "this call gated" fact is visible in 04's telemetry
  (`gated_questions`), not in `sense()`.

This write rule is specified here as the binding joint contract; the 04
reviser must adopt per-key merge (their red-team already recommends
exactly this) — whole-cache replacement with the gated view is
incompatible with it.

### 3.4 What "stale-but-flagged" means for the downstream planner

The value **does reach the WorldState** (via `SensorManager.update_state`
/ `update_state_detailed`, which set each key as today). Rationale: the
planner must never silently plan on data the host believes is fresh —
but dropping a key the planner's preconditions depend on is *also* a
behavior change, and 0.5.0's contract is that the cache keeps the loop
alive until `max_stale`. The ladder's middle band therefore means
"present, but labeled."

The label travels **outside** the WorldState:

- `SensorReport.meta[key]` carries `ValueMeta(age_seconds, staleness,
  source, as_of_monotonic)`.
- We do **not** write sibling keys like `danger__stale` into the
  WorldState, and we do **not** wrap values (no `Stale(value)` wrapper
  type): both would leak into `diff()`, hashing, JSON serialization, and
  every precondition/effect comparison the planner does. The WorldState
  stays a plain value bag; freshness is side-channel metadata.

Consumers that care read the report: 01 via `max_age()`, 03 via
`last_report()` transitions, 06 via `diagnostics()`/`health()`.
Consumers that don't see 0.5.0 behavior.

### 3.5 Sense-on-change with semantic diffing

Correction to the premise: Python `dict.__eq__` is already **deep** for
nested containers (`{"a": [1, {"b": 2}]} == {"a": [1, {"b": 2}]}` is
`True`). What 0.5.0's `observation != self._last_observation` lacks is
not depth but **semantic tolerance**: float noise (`0.30000000000000004`
vs `0.3`), key-order/representation churn, structurally different but
semantically identical observations.

Design: `JevSensor(..., observation_fingerprint:
Callable[[dict], Any] | None)`. When set, `_should_resense` compares
`fingerprint(observation) != fingerprint(last_observation)` instead of
raw dict equality. When `None` (default), raw `==` — 0.5.0 behavior,
per §3.2 rule 1.

The hook — not a shipped helper — is the seam. (The previous revision's
`tolerant_fingerprint` rounding helper is cut: it baked in one
tolerance semantic, and its NaN behavior — NaN leaves never compare
equal, so NaN-containing observations re-sense forever, silently
defeating `min_interval` — is exactly the kind of blessed footgun the
library should not ship. Hosts with float noise write ten lines.)

Hook contract, precise:

- the fingerprint must be **pure, deterministic, and cheap**; it runs on
  every `sense()` call;
- its return value must support `==` (hashability recommended);
- a fingerprint that **raises propagates** (fail-loud — a broken
  change-detector must not silently freeze re-sensing);
- **NaN caveat, documented not validated:** NaN never equals NaN, so an
  observation containing NaN re-senses on every call. Fingerprints must
  be NaN-free or NaN-normalizing; the resulting behavior is the host's
  responsibility;
- with `resense_on_change=False` the fingerprint is ignored
  (documented, not an error).
- `CachingSensor.on_change` is the same idea applied to the wrapped
  sensor's output dict: when the fingerprinted output is unchanged, the
  success path still serves it but does **not** refresh `as_of`
  timestamps — a sensor re-emitting identical readings must not refresh
  its own TTL and mask a genuinely frozen upstream. Default `None` →
  every successful `inner.sense()` refreshes timestamps.

### 3.6 Per-key staleness (the "one slow question" problem)

- `JevSensor`'s cache becomes per state-key: each entry stores
  `(value, as_of_monotonic)`. A re-judge refreshes the keys it answers
  per §3.3 (one `system_one` call still answers all questions — no
  per-question API fan-out; that would multiply cost and latency).
- The per-key win is on the **failure path** and the **policy path**:
  keys age out independently, so a slow-moving judgment (`"room_name"`)
  can stay flagged-but-usable while the cache around it churns.
- `CachingSensor` is per-key from birth: it timestamps each key of the
  wrapped sensor's dict independently. Keys that disappear from the
  wrapped output keep their old value under the ladder until DEAD, then
  vanish from `sense()` output (the WorldState retains the last
  *written* value — §3.8 edge 1, decided).
- **Mixed freshness in one `sense()` dict** is normal: `meta` tells
  which keys are FRESH vs STALE; the dict itself is unchanged in shape.

### 3.7 `SensorManager` aggregation semantics

- `update_state_detailed` calls `sense()`/`asense()` on every sensor in
  order and merges dicts with `dict.update` order = sensor order (same as
  today: later sensors win on key collision). Reports are returned in the
  same order; on key collision the *winning* sensor's `ValueMeta` is the
  one a merged view would show — but every sensor's own report is
  preserved in the list, so no metadata is lost.
- `health()` calls each sensor's `health()`; a sensor whose `health()`
  raises contributes the worst-case entry (§2.5); the aggregate view
  never breaks because one sensor is broken.
- Fail-loud is preserved: `update_state[_detailed]` lets sensor
  exceptions propagate exactly as 0.5.0. `health()` is the non-raising
  introspection path; the update path is the raising execution path. Both
  exist deliberately.

### 3.8 Edge cases

1. **WorldState retains the last written value for DEAD keys — DECIDED.**
   When a key goes DEAD we omit it from `sense()` output;
   `SensorManager` then does not set it, so the WorldState keeps
   whatever was last written. This is 0.5.0 behavior (returning `{}`
   never cleared keys) and it stays: manager-side unsetting would be
   silently dropping keys the planner depends on — the exact thing
   non-goal 3 forbids — and it would contradict 04's non-goal against
   deleting keys. The honest cost, stated plainly: a DEAD key is
   **indistinguishable from FRESH in the WorldState** — the planner
   will act on a value the design declares unusable, with the only
   signal in the side channel. Mitigation within this design's
   principles: treat the STALE band as the action point (hosts
   replan/verify on STALE so keys rarely reach DEAD) and point hosts at
   `health().dead_keys`. (Former open question §7.1, closed.)
2. **`stale_after` with `min_interval`:** see §3.1 — routinely STALE
   between re-queries is the operator's explicit choice.
3. **`resense_on_change=False` + fingerprint:** fingerprint ignored
   (documented, not an error).
4. **Clock:** all aging uses `time.monotonic()`, consistent with 0.5.0.
   `ValueMeta.as_of_monotonic` is monotonic; hosts correlating with wall
   time convert at read time (see §7 open question 1).
5. **Empty `sense()` dict from inner sensor:** refreshes nothing; existing
   cached keys keep aging per §3.3. An inner sensor returning `{}` forever
   lets its keys die on schedule — correct, since no new information
   arrived.
6. **`judge()` shares the ladder:** `JevSensor.judge` uses the same
   per-key cache and bands as `sense()`; injected observations get
   `source=<sensor name>` with `as_of` at injection time. `sense()` and
   `judge()` must not be mixed on one instance — and must not be called
   concurrently from multiple threads on one instance (0.5.0
   not-thread-safe rule, unchanged and now un-hedged: §1 non-goal 1).
7. **Telemetry callback exceptions** stay swallowed-with-log (0.5.0 rule).
8. **`max_stale` vs `stale_policy=DROP`:** `DROP` moves the cliff from
   `max_stale` to `stale_after`; keys past `max_stale` are still omitted
   (the outer bound never moves).
9. **`stale_cache_hits` counts failure-path replays only** (0.5.0
   semantics, unchanged). STALE-band service on the success path (the
   `min_interval` skip path with `stale_after` set) does not touch the
   counter and does not emit `stale_cache_hit=True` telemetry — it is
   visible via `last_report()` / `diagnostics()` bands instead. This is
   what lets 06's stats-delta detection keep meaning "served after a
   failure" while the ladder's policy-driven STALE service stays
   separately observable.
---

## 4. Failure-mode table

| Failure mode | Handling |
|---|---|
| Wrapped/`observe` callable raises, cache has usable keys | Serve per-key ladder (FRESH/STALE served, DEAD omitted); record error class in health; telemetry `error` set, `stale_cache_hit=True` on the failure path (0.5.0 semantics; §3.8 edge 9) |
| Sensor raises, no usable cache (first call, or all keys DEAD) | Original exception propagates — fail-loud, as 0.5.0 `SensorManager` |
| TypeSafe call fails inside `JevSensor` | Per-key ladder: keys within their band thresholds served with flags; past `max_stale` → key omitted (0.5.0 served the whole `{}`; per-key is the generalization) |
| TypeSafe returns answer missing a mapped question | `TypeSafeError` as today (response-shape bug, not staleness — fail loud) |
| Non-`TypeSafeError` from the client | Propagates with no telemetry record (0.5.0 behavior) |
| Fingerprint callable raises | Propagates (fail-loud); a broken change-detector must not silently freeze re-sensing |
| Telemetry callback raises | Swallowed with `logger.exception` (0.5.0 rule) |
| `health()` on a sensor raises internally | `SensorManager.health()` catches per sensor → worst-case entry (`status="dead"`, `error="health_failed:<ExcClass>"`); the aggregate view never breaks |
| `update_state_detailed` sensor raises | Propagates (fail-loud, same as `update_state`); partial reports list is discarded — no half-merged metadata |
| Async sensor's `asense` raises | Propagates from `aupdate_state` (fail-loud parity with sync) |
| Sensor implements neither `asense` nor `sense` on the async path | `TypeError` with a clear message |
| Invalid policy (`stale_after > max_stale`, negative, `max_stale < 0`) | `ValueError` at construction |
| Clock jump (monotonic is monotonic — N/A) | `time.monotonic()` never goes backwards; no handling needed |

---

## 5. Interaction notes with the other five differentiators

**(a) Runtime / replan budgets (01).**
Staleness is an input to budget pressure, not a budget itself. The
seam is exact: the host calls `reports =
mgr.update_state_detailed(state)` and passes
`sensor_age=max_age(reports)` into 01's `should_replan(...)`.
`max_age` is the oldest per-key age in the current sense cycle, `None`
when there are no keys (01 treats `None` as unknown/fresh — its own
semantics, unchanged). `oldest_staleness(reports)` is the worst band
for hosts that want the band, not the scalar. The scalar cannot express
per-key granularity — that is a documented limitation, not a gap: 01's
`SENSORS_STALE` gate is all-or-nothing ("don't replan world-change
diffs computed from garbage"), and the conservative scalar for that
gate is the maximum age. Hosts needing per-key policy read
`SensorReport.meta` directly. One concrete hook: `SensorManager.health()`
gives the budget loop a cheap pre-plan check without touching the
WorldState. Note for the 01 reviser: under the ladder, `JevSensor`
degrades *gradually* (STALE served, flagged) and reaches `{}` only past
`max_stale` — 01's "degrades to `{}`" guidance should read as the end
of the ladder, not the whole story.

**(b) Action interruption (03).**
Interruption needs *transitions*, not just current band. The contract:
the interruption worker keeps the previous `SensorReport` and diffs
per-key `(staleness, as_of_monotonic)` against the current
`last_report()` to detect FRESH→STALE and STALE→DEAD transitions on
keys the running action depends on. It must **not** diff
`age_seconds` — that field is frozen at report-build time and decays
immediately after. We do not emit events ourselves (no event bus exists
in 0.5.0; the diagnostics design owns the event vocabulary — see the
producer contract, §9). Recommended wiring: the interruption checker is an `on_action_complete`
hook closure that closes over the sensor and the `PlanExecution`
handle (03's seam — `interrupt_when` was cut from the design; there is
no predicate seam). After each action the closure diffs the previous
`SensorReport` against the current `last_report()` and calls
`execution.interrupt(reason, source="sensor")` on FRESH→STALE or
STALE→DEAD transitions for keys the running action depends on.

**(c) Confidence gating (04) — conjunction.**
Staleness and confidence are independent axes; a judgment is usable iff
it passes both gates (confidence ≥ threshold at answer time, age within
policy at replay time). 04's gating is implemented as the per-key write
filter specified in §3.3 (gated ⇒ treated as absent-from-output: not
refreshed, not deleted). No `staleness_discount`, no multiplicative
composition — cut per ruling A.

**(d) Provider-independent judgment interface (05) — where caching lives.**
Caching lives in a layer **above** the judge protocol:

- The judge protocol (whatever the (d) worker names it — e.g.
  `JudgeBackend.judge(observation) -> dict`) stays a pure function of
  the observation: no time, no cache, no staleness. Every backend
  (TypeSafe, NVIDIA NIM, local model, fake) gets caching for free because
  caching wraps the protocol, never implements it.
- Module home: `goapauto/models/caching.py` — `CachePolicy`,
  `CachingSensor`, `Staleness`, `ValueMeta`, `SensorReport`,
  `SensorHealth`, `AsyncSensor`, `max_age`, `oldest_staleness`.
  `JevSensor` adopts the policy types for its own cache (thin parameter
  translation: `stale_after`/`stale_policy` → `CachePolicy`) rather than
  growing a parallel implementation; the default-mode equivalence
  contract (§3.2) plus the differential harness (§6.1) is the proof that
  the shared machinery does not drift from 0.5.0.
- `JevSensor.judge`, `shared_client`, `FakeTypeSafeClient` signatures and
  semantics are untouched (stability requirement).
- **Convergence note for the 05 reviser:** 05 plans Phase-2 delegation
  of `JevSensor` onto `JudgmentSensor`+`JevJudge`. That layer must reuse
  these cache types (`CachePolicy`, the per-key write rule of §3.3),
  not re-specify caching — the fork 05 §2.6 warns against. This design
  owns `CachePolicy` in Phase 1; 05 owns the judge protocol.

**(e) Why-diagnostics (06) — the producer contract.**
06 owns the trace format; 02 guarantees the facts. The binding contract
is §9: exact `health()` shape, exact `diagnostics()` keys, the
`fresh`/`stale`/`dead` vocabulary, per-key ages in seconds (float,
monotonic-derived), the staleness payload table, and what 06 must not
assume. 06's old whole-cache sketch keys (`data_age_ms`,
`cache="hit"|"miss"|"stale"`, `sensor_stale: {sensor, age_ms,
max_stale_ms, keys}`) are superseded by that vocabulary — the 06
reviser adapts the schema to it.
---

## 6. Test plan sketch (100% coverage gate)

The suite must cover, at minimum:

1. **0.5.0-equivalence harness (binding).** Two parts. (a) Differential
   fuzz: a frozen reference model of 0.5.0 `_judge`/`_should_resense`
   (vendored in the test module, ~30 lines, transcribed from the 0.5.0
   source) versus `JevSensor` with defaults, over a seeded randomized
   script: changing/unchanging observations, client success /
   `TypeSafeError` / non-`TypeSafeError`, monkeypatched monotonic clock
   including the `min_interval > max_stale` skip-path edge. Assert
   identical `sense()` return sequences, `stats()` counters, full
   `JevCallRecord` streams (every field), and `system_one` call logs.
   (b) Call-pattern pinning: the existing `test_jev.py` scenarios re-run
   against the new implementation with defaults and `FakeTypeSafeClient`,
   asserting identical call counts and arguments. The legacy suite
   staying green is necessary, not sufficient.
2. **Ladder transitions at exact boundaries** — monkeypatched
   `time.monotonic`: `age == stale_after` → FRESH; `age == stale_after +
   ε` → STALE; `age == max_stale` → STALE (served); `age == max_stale +
   ε` → DEAD (omitted). Both `KEEP_FLAGGED` and `DROP`.
3. **Per-key independence** — two keys, one fails into STALE while the
   other re-senses FRESH; a key absent from a later successful output
   keeps its old `(value, as_of)` and keeps aging (the §3.3 write rule,
   which is also the seam 04's write filter exercises).
4. **Metadata channel integrity** — `sense()` return contains no
   metadata keys; `set(report.meta) == set(report.values)`;
   `ValueMeta` fields populated; DEAD keys absent from both.
5. **WorldState non-pollution** — after `update_state_detailed` with
   STALE keys, `state.to_dict()` / `diff()` / `hash()` show only plain
   values; no `__stale` suffix keys, no wrapper types.
6. **Fail-loud paths** — inner sensor raises with empty cache →
   original exception propagates (identity check, not just type);
   `update_state_detailed` raising mid-list discards partial reports;
   non-`TypeSafeError` propagates with no telemetry record.
7. **`health()` aggregation** — mixed sensors (caching, non-caching,
   failing-health): statuses, `stale_keys`/`dead_keys` tuples,
   worst-case entry for a raising `health()`, plain-sensor
   `fresh`/`None` entries, and `health()` never raising.
8. **Fingerprint semantics** — custom fingerprint used for change
   detection; raising fingerprint propagates; fingerprint ignored when
   `resense_on_change=False`; `CachingSensor.on_change` freezes TTL
   refresh on identical output; NaN-containing observation re-senses
   every call (documented behavior, pinned).
9. **Async** — `aupdate_state[_detailed]` with a fake `AsyncSensor`;
   sync-only sensor on the async path is called inline (no thread hop);
   a sensor implementing neither raises a clear `TypeError`.
10. **`diagnostics()` shape** — exact keys, `{}` before first sense,
    JSON-safe (round-trips through `json.dumps`), per-key ages and
    bands match the last report.
11. **`max_age` / `oldest_staleness`** — `None`/`FRESH` on empty input;
    max semantics across sensors; worst-band ordering.
12. **Validation** — `stale_after > max_stale` → `ValueError`; negative
    `stale_after` → `ValueError`; `max_stale < 0` → `ValueError`
    (both `CachePolicy` and `JevSensor` params).
13. **`last_report()` lifecycle** — `None` before first sense; reflects
    the most recent `sense()`/`sense_detailed()` call; `judge()` shares
    the ladder (injected observation timestamped at injection).
14. **DEAD retention (decided)** — dead keys omitted from `sense()` but
    the WorldState keeps the last written value; `dead_keys` visible in
    `health()`.
15. **Default-mode skip-path edge** — `min_interval > max_stale`: the
    skip path serves the cache with no age check (0.5.0-faithful); band
    reported FRESH, `age_seconds` exact and exceeding `max_stale`.
16. **`stale_cache_hits` scope** — failure-path replays increment it;
    success-path STALE service (skip path with `stale_after` set) does
    not.

Coverage notes: no `pragma: no cover` in source (project rule). Time is
monkeypatched via a fake clock fixture — never `time.sleep` in tests.
The equivalence harness (§6.1) is the backward-compat proof; the legacy
suite is a necessary condition only.

---

## 7. Open questions (recorded, not guessed)

1. **~~Should `SensorManager` unset DEAD keys from the WorldState?~~
   DECIDED — keep 0.5.0 retention.** The manager never unsets; the
   WorldState keeps the last written value. Decided by non-goal 3 (a
   manager that silently drops keys is a second, hidden planner) and
   consistent with 04's non-goal against deleting keys. See §3.8 edge 1
   for the documented hazard and mitigation.
2. **Monotonic vs wall-clock in trace payloads.** `ValueMeta` uses
   monotonic (consistent, jump-proof). Diagnostics correlating with
   external logs may want wall time too. Options: (a) add
   `as_of_wall: float` (time.time()) to `ValueMeta`; (b) leave conversion
   to the diagnostics worker. Cost of (a) is one field; benefit is no
   clock-domain bugs downstream.
3. **Where does the STALE→DEAD transition get *acted* on?** This design
   exposes transitions via `last_report()` diffing on
   `(staleness, as_of_monotonic)`. If the interruption or budgets workers
   want push-style callbacks (`on_staleness_transition`), that is their
   API to request — we will add the hook, but we will not invent the
   event bus here.
4. **Module naming.** `goapauto/models/caching.py` is proposed; the
   implementer may prefer `goapauto/models/sensor_cache.py`. Either is
   fine — the public surface goes through `goapauto/__init__.py`
   `__all__` regardless.
5. **Should `stale_after` default to something non-None in a future
   major?** 0.5.0 parity demands `None` now. A future 1.0 could default
   `stale_after = max_stale / 2`, but that changes what "no
   configuration" means — flagged for the 1.0 API review, not decided
   here.
---

## 8. Adversarial review — disposition of all 14 attacks

Every numbered attack from
`reviews/02-sensor-caching-staleness-review.md` is resolved, rejected
with reasoning, or deferred with an owner. Nothing silently dropped.

1. **02/04 composition contradiction (multiplicative decay vs
   conjunction).** RESOLVED. Ruling A: conjunction wins. §5(c) rewritten;
   `staleness_discount` cut entirely — not kept as an informational
   helper. Justification for the cut over the keep: the helper's sole
   purpose was a composition the gating design refused; keeping it
   invites re-litigation and a second, divergent code path for the same
   decision.
2. **`SensorReport`/`ValueMeta` have no consumer.** RESOLVED. Named
   in-core consumers, each with a branching use: 06 merges
   `diagnostics()` into its `sensor_update` trace payload and reads
   `health()`/`last_report()` for staleness events; 01 consumes
   `max_age()`/`oldest_staleness()` as its `sensor_age` scalar; 03's
   interruption checker branches on per-key FRESH→STALE / STALE→DEAD
   transitions diffed from `last_report()`. The sharp question is
   answered: 03 branches on `ValueMeta.staleness` transitions, 06
   branches on the `stale`/`dead` bands for its events.
3. **Sanctioned cross-thread `judge()` pattern is a data race.**
   RESOLVED. Ruling G: the pattern is retracted, not repaired. §1
   non-goal 1 now states there is no supported prefetch story in Phase
   1, and the 292ms-on-16ms-tick consequence is stated honestly instead
   of papered over. The async story is the slim `aupdate_state()`
   (synchronous ownership, no shared mutation).
4. **`JevCallRecord` field-order collision with 04.** RESOLVED. This
   design adds no fields to `JevCallRecord`; ownership of the record's
   evolution is ceded to 04, which touches it more deeply. 02's band
   information travels via `SensorReport` / `health()` /
   `diagnostics()` instead. (The 04 review noted our additive field
   "composed cleanly" — ceding is cleaner than a handshake field order
   neither side can enforce alone.)
5. **Per-key write rule vs 04's gated-view replace.** RESOLVED. §3.3
   specifies the binding per-key write rule (absent keys keep
   `(value, as_of)`; a successful call never deletes entries) and the
   joint contract: 04's gating is a per-key write filter (gated ⇒
   treated as absent-from-output: not refreshed, not deleted). Flagged
   as a joint contract the 04 reviser must adopt — their red-team
   already recommends exactly this merge rule.
6. **`aupdate_state` overcorrects (sync sensors unusable).** RESOLVED.
   Adopted the fix: `aupdate_state[_detailed]` awaits `asense()` where
   present, calls `sense()` inline otherwise (no thread hop involved);
   `TypeError` only when a sensor implements neither.
7. **DEAD keys — take the side.** RESOLVED per coordinator ruling: keep
   0.5.0 retention. Former §7.1 closed as decided, justified by non-goal
   3 (manager-side unsetting *is* the "second, hidden planner" the
   non-goal forbids) and 04's non-goal against deleting keys. The hazard
   (DEAD indistinguishable from FRESH in the WorldState) is documented
   in §3.8 edge 1 with the mitigation (STALE as the action band,
   `health().dead_keys`).
8. **`per_key` typo-silent config.** RESOLVED. `per_key` overrides cut
   from Phase 1 — no such config exists, so no never-arriving-key
   hazard exists. Per-key *aging* (automatic for all keys) already
   delivers the goal's win.
9. **`age_seconds` decays at read time.** RESOLVED. §2.1 documents
   `age_seconds` as build-time-only; §5(b) specifies the transition
   contract on `(staleness, as_of_monotonic)`, never `age_seconds`.
10. **Backward-compat proof asserted, not constructed.** RESOLVED. §3.2
    is now a binding, line-by-line equivalence contract derived from
    the 0.5.0 source, and §6.1 pins it with a differential fuzz harness
    (frozen 0.5.0 reference model + call-pattern pinning against the
    existing suite with `FakeTypeSafeClient`). The doc states explicitly
    that the legacy suite alone is not the proof.
11. **Async surface vs 05's "no async in Phase 1".** PARTIALLY RESOLVED
    / DEFERRED. The manager seam (`AsyncSensor`, `aupdate_state[_detailed]`
    with the attack-6 fix) is kept per ruling G's sanction of
    `aupdate_state()` as the async story; `to_async` is cut (it blesses
    the thread hop ruling G killed). No in-tree Phase-1 producer exists
    — 05's future `AsyncJudge` is the deferred owner of the producer
    side.
12. **Over-engineering cuts.** RESOLVED. Cut: `staleness_discount`
    (attack 1), `to_async` (attack 11), `per_key` overrides (attack 8),
    `tolerant_fingerprint` helper (hook kept), the `SensorHealth.known`
    flag (status + nullable fields suffice). Kept: the ladder bands,
    `CachingSensor`, `health()` aggregation, `sense_detailed()` (now
    with named consumers per attack 2), monotonic aging,
    `stale_after=None` default.
13. **Sketch lint (`typing.Callable`/`typing.Protocol`).** RESOLVED.
    Sketches now use `collections.abc.Callable` and `typing.Protocol`
    per the repo's `collections.abc` convention (matching `jev.py`); the
    `__all__` delta is enumerated exactly in §2.9.
14. **`min_interval` vs `stale_after` footgun.** RESOLVED (documented)
    / REJECTED (warning). §3.1 now states up front that with default
    `min_interval=0.0` the ladder is failure-path-only (the 0.5.0
    re-judge-every-tick quirk, preserved). The proposed construction
    warning for `stale_after < min_interval` is rejected: its premise is
    inverted — that configuration is coherent (STALE routinely served
    between throttled re-queries *by design*), not contradictory.

Cross-doc items from the 04 review touching 02: 04-attack-7
(fully-gated `{}` vs dead-cache `{}` conflation) — resolved from 02's
side by the §3.3 write rule (all-gated with a warm cache serves aging
cached values per the ladder, not `{}`; with an empty cache both cases
return `{}`, distinguished via 04's `gated_questions` telemetry and
02's `diagnostics()["dead_keys"]`); 04-attack-8 (gate-kept values' age
discarded) — resolved by the per-key non-refresh rule (gated keys keep
their `as_of` and age honestly toward STALE/DEAD). 04-attack-13
(`plan_confidence` producer) is 01/04's scope, not 02's.

---

## 9. Producer contract (binding for 06 and 01)

Exact contract this design guarantees to consumers. The 06 reviser
builds the diagnostics schema against this section; the 01 reviser
builds the replan policy's sensor-age input against it. Field names,
types, and vocabulary below are binding — implementations must match
them literally.

### 9.1 Vocabulary

- Staleness states are exactly the strings `"fresh"`, `"stale"`,
  `"dead"` — the values of the `Staleness` enum. `ValueMeta.staleness`
  is the enum; every trace-facing payload uses `staleness.value`.
  `SensorHealth.status` is typed `str` and holds one of the three
  literals.
- Per-key ages are seconds (`float`), derived from `time.monotonic()`.
  No milliseconds anywhere in this design's surface. (`latency_ms`
  keeps its 0.5.0 name and unit — it predates this design and is out of
  scope to rename.)
- Key collections (`stale_keys`, `dead_keys`) are tuples in cache
  insertion order.

### 9.2 `SensorManager.health() -> list[SensorHealth]`

In sensor order. Never raises. Exact shape:

```python
@dataclass(frozen=True)
class SensorHealth:
    name: str  # name or type(sensor).__name__ (§2.4)
    status: str  # "fresh" | "stale" | "dead" — worst band across keys
    last_success_age: float | None  # seconds since last successful
    # re-sense; None if never succeeded (or sensor has no cache clock)
    stale_keys: tuple[str, ...]  # keys currently in the STALE band
    dead_keys: tuple[str, ...]  # keys currently DEAD (omitted from sense())
    error: str | None  # exception class name from the most recent failed
    # sense; None if the last sense succeeded or none has run
```

Degraded entries: a sensor whose `health()` raises contributes
`SensorHealth(name=name, status="dead", last_success_age=None,
stale_keys=(), dead_keys=(), error="health_failed:<ExcClass>")`.
Plain (non-caching) sensors contribute `status="fresh"`,
`last_success_age=None`, empty key tuples, `error` as observed —
"fresh" here means "computed live; no cache clock," not a data-age
claim.

### 9.3 `SensorReport` / `ValueMeta`

```python
@dataclass(frozen=True)
class ValueMeta:
    age_seconds: float  # at report-build time ONLY; recompute for decisions
    staleness: Staleness  # wire vocabulary: staleness.value
    source: str  # sensor name when live-sensed this call, else "cache"
    as_of_monotonic: float


@dataclass(frozen=True)
class SensorReport:
    values: dict[str, Any]  # exactly what sense() returned
    meta: dict[str, ValueMeta]  # invariant: set(meta) == set(values)
    sensor: str
```

DEAD keys are absent from both `values` and `meta`. `last_report() ->
SensorReport | None` is `None` before the first `sense()`; otherwise
it reflects the most recent `sense()` / `sense_detailed()` / `judge()`
call.

### 9.4 `diagnostics() -> dict[str, Any]` (duck-typed; 06 merges into `sensor_update`)

Exact keys, JSON-safe, never raises, `{}` before the first report:

```python
{
    "data_age_seconds": {key: float, ...},
    # per-key age at last report build (monotonic-derived)
    "staleness": {key: "fresh" | "stale", ...},
    # per-key band, same key set; DEAD keys absent
    "dead_keys": [key, ...],
    # keys currently DEAD
    "worst_staleness": "fresh" | "stale" | "dead",
    # worst band across keys
}
```

Present on `CachingSensor` and `JevSensor`. 06 must not assume the old
sketch keys `data_age_ms` or `cache="hit"|"miss"|"stale"` — superseded
by this vocabulary.

### 9.5 Staleness payloads 06 may consume (exact field names)

06 owns the trace schema; these are the facts 02 guarantees are
derivable from `last_report()` / `diagnostics()` / `health()` at each
cache-relevant moment:

| moment | fields (exact names) |
|---|---|
| live re-sense served | `sensor`, `keys`, `latency_ms`, `staleness="fresh"` (per key) |
| failure-path cache replay | `sensor`, `keys`, `age_seconds` (per-key map), `staleness` (per-key map), `error` |
| STALE band served (policy, no failure) | `sensor`, `keys`, `age_seconds` (per-key map), `staleness="stale"` |
| key went DEAD (omitted) | `sensor`, `keys`, `last_age_seconds` (per-key map) |
| re-sense skipped: within `min_interval` | `sensor`, `since_last_call_s` |
| re-sense skipped: fingerprint unchanged | `sensor` |

What 06 must NOT assume: `JevStats.stale_cache_hits` counts
**failure-path replays only** (§3.8 edge 9) — ladder-band STALE service
on the success path never touches it, so 06's stats-delta detection
keeps meaning "served after a failure" while policy-driven STALE
service is observed via the bands above. `as_of` values are monotonic;
wall-clock correlation is 06's choice (see §7.2).

### 9.6 What 01 may consume

`max_age(reports) -> float | None`: oldest per-key `age_seconds`
across the `update_state_detailed` report list; `None` when there are
no keys. The host passes it as 01's `sensor_age` ("seconds since last
good sensor data; `None` = unknown/fresh" — 01's semantics, unchanged).
`oldest_staleness(reports) -> Staleness`: worst band across reports
(`dead > stale > fresh`); `FRESH` when empty. Both are pure functions
of the report list; 01 imports no sensor types.

### 9.7 What 03 may consume

`sensor.last_report()` before/after each `update_state_detailed`: per-key
`(staleness, as_of_monotonic)` transitions. 03 must not compare
`age_seconds` across reports (build-time-frozen). No event bus is
introduced by this design; transition detection is poll-based by
contract.
