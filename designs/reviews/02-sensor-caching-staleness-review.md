# RED-TEAM REVIEW — Design 02: Sensor caching & stale-data behavior

**Reviewer:** red-team (hostile read)
**Target:** `~/workspace/goapauto-work/designs/02-sensor-caching-staleness.md`
**Date:** 2026-09-21
**Scope:** design doc only; no source modified, no state changed.

---

## Verdict: NEEDS-REWORK

The ladder mechanics are carefully specified, but the design has structural
faults that cannot be fixed at implementation time: (1) its staleness×confidence
composition rule **directly contradicts** Design 04's composition rule — both
docs claim ownership of the same multiplication and define it incompatibly;
(2) the parallel metadata channel (`SensorReport`/`ValueMeta`) has **no
consumer in core** and none of the four designs that are supposed to consume
it accept its types; (3) the non-goal justification for rejecting background
threads blesses a host pattern (`judge()` on a refresh thread) that violates
the library's own not-thread-safe contract. These are design-level
contradictions, not polish issues.

---

## The three strongest attacks

1. **Design 02 §5(c) and Design 04 §5(b) define mutually exclusive
   staleness×confidence compositions.** 02 says
   `effective_confidence = confidence * staleness_discount(meta)` with linear
   decay across the STALE band; 04 says "Staleness does **not** decay
   confidence… The two compose by conjunction." Both cannot ship.
2. **The metadata channel is dead weight: nobody consumes `SensorReport`.**
   The planner reads `WorldState`, which `update_state` fills with STALE
   values as plain values. Designs 01, 03, 04, 06 — the named consumers —
   take `sensor_age: float`, `(state, next_action)`, gate-at-extraction, and
   a duck-typed `diagnostics()` mapping respectively. None of them ingests
   `SensorReport`/`ValueMeta`.
3. **The "sanctioned" host prefetch pattern is a data race.** §1 non-goal 1
   rejects library threads but blesses hosts calling `JevSensor.judge()` on
   their own refresh thread — while `judge()` mutates the same
   `_cached`/`_last_success` state that the tick thread's `sense()` reads,
   on a class documented "not thread-safe", with an explicit rule (§3.6.6)
   that `sense()` and `judge()` must not be mixed on one instance.

---

## Numbered attacks

### Attack 1 — The 02/04 composition contradiction (hard fail)

**Flaw.** Design 02 §5(c) proposes `staleness_discount(meta)` — 1.0 when
FRESH, linear decay 1.0→0.0 across the STALE band — and states "The gating
worker multiplies its per-value confidence by this discount." Design 04 §5(b)
states the opposite as a deliberate decision: "Staleness does **not** decay
confidence. Confidence is a property of the judgment at answer time; aging
is the staleness design's axis… The two compose by conjunction: a judgment
reaches the world state only if it was confident-enough when made AND
fresh-enough when replayed."

**Why it matters.** Concrete scenario: `threat` judged at confidence 0.9,
threshold 0.6, now 20s old with `stale_after=5, max_stale=30`. Under 02's
rule the effective confidence is `0.9 * (1 - 15/25) = 0.36` → gated. Under
04's rule it passes the gate (0.9 ≥ 0.6) and is served STALE-but-flagged.
Two shipped designs give opposite answers on the same input, and 04's
rationale ("a step at `stale_after` would reintroduce a cliff") is aimed
directly at 02's linear decay. This is not a gap — it is a collision, and
04's §5(b) reads as if it was written *against* 02's §5(c).

**Fix or question.** One of the two must yield, explicitly, with the loser
deleting its rule. My read: 04's conjunction is the cleaner composition
(decay reintroduces the cliff the ladder was built to remove, exactly as 04
argues), which means **cut `staleness_discount` from 02 entirely** — it is
also the only function in 02's API whose sole purpose is to be consumed by
another design that has refused it.

### Attack 2 — `SensorReport`/`ValueMeta` have no consumer (dead weight)

**Flaw.** §3.2 is proud that staleness metadata "travels on a channel that
never silently pollutes WorldState keys" — but the primary consumer, the
planner, reads only `WorldState`, and `update_state` (which §2.5 leaves
"untouched") writes STALE values into it as plain, unflagged values. The
design's answer is "consumers that care read the report" — yet:
- Design 01 consumes staleness as `sensor_age: float | None` (§2.4,
  `should_replan(..., sensor_age=...)`) — a scalar, not a report. Its §5(a)
  says "The policy takes a plain `float`, never `JevStats`" — and never a
  `SensorReport` either. 02's §5(a) proposes `max_age()`/`oldest_staleness()`
  helpers; 01 never mentions them.
- Design 03's seam is `interrupt_when(state, next_action) -> str | None` —
  no sensor, no report. 02's §5(b) "recommended contract" (diff
  `last_report()` metas) is host glue code, not a consumed API.
- Design 04 gates at extraction, before the cache (§5b "gate at write,
  replay at read") — the STALE band plays no role in its logic.
- Design 06 wants a duck-typed `diagnostics() -> Mapping[str, Any]` on the
  sensor (§6b) — 02 provides `health()` and `last_report()`, not
  `diagnostics()`. The vocabularies do not meet.

**Why it matters.** Concrete scenario: Soulscape's tick loop calls
`mgr.update_state(state)` exactly as in 0.5.0 (the design promises this
keeps working "byte-for-byte"). A `JevSensor` with `stale_after=5` serves a
12-second-old `enemy_distance` into the WorldState. The planner acts on it.
No flag is visible anywhere in the planner's input — the "present, but
labeled" claim of §3.2 is false for every consumer that didn't rewrite its
loop to the `_detailed` API *and* thread reports into policy. The design
exports facts and assigns every policy to other designs that don't import
them. The ladder's middle band is therefore observable only via polling
`health()`/`last_report()` — a host convention, not a library contract.

**Fix or question.** Either (a) admit the ladder is a host-side convention
and cut `ValueMeta`/`SensorReport`/`sense_detailed()`/`update_state_detailed()`
down to `health()` alone, or (b) give the manager a real in-band signal —
e.g. `update_state_detailed` is not enough; the *planner-visible* path needs
it. Sharp question for the designer: name one in-core code path, existing
or proposed in any of the six designs, that *branches* on
`ValueMeta.staleness`. If none exists, the parallel channel is speculative
machinery and YAGNI applies.

### Attack 3 — The sanctioned prefetch pattern is unsound (hard fail)

**Flaw.** §1 non-goal 1 rejects background refresh threads ("would need
locks around the cache, the WorldState, and the planner loop") and then
blesses the host alternative: "Hosts that want prefetch already have a
sanctioned pattern: refresh on their own thread and inject the snapshot
(`JevSensor.judge(observation)` exists precisely for this)."

But `judge()` → `_judge()` mutates `self._cached`, `self._last_observation`,
`self._last_call`, `self._last_success`, and the telemetry counters — the
exact same state the tick thread's `sense()` reads. `JevSensor` documents
"not thread-safe. Use one JevSensor per thread." §3.6.6 adds "sense() and
judge() must not be mixed on one instance." A host following the sanctioned
pattern gets cross-thread `sense()`/`judge()` on one instance: a data race
on `_cached` (dict replaced mid-read → `RuntimeError` or torn reads) and on
`_last_success` (staleness computed from a torn timestamp).

**Why it matters.** Concrete scenario: Soulscape runs the tick loop at
16ms and a refresh thread calling `sensor.judge(obs)` every second to hide
the 292ms TypeSafe p50. Every ~1s the tick thread's `sense()` iterates
`self._cached` while the refresh thread replaces it — CPython makes the
rebind atomic but the *read-modify* sequences in `_should_resense` and the
telemetry counters are not. Worse: there is no race-free host pattern at
all, because nothing lets a host inject *answers* — `judge()` takes an
observation and still performs the judgment call on the calling thread. The
"sanctioned pattern" doesn't even achieve prefetch; it just moves the
292ms call to another thread while keeping the race.

**Fix or question.** Either the design owns a lock (contradicting the
not-thread-safe contract — don't), or it stops blessing the pattern and
states plainly: *there is no supported prefetch story in Phase 1; hosts
that need one copy judgments out-of-band and construct a fresh sensor, or
accept `min_interval` quantization.* Sharp question: walk through the
memory-visibility story of one `judge()` on thread B concurrent with one
`sense()` on thread A and show where the happens-before edge is. There
isn't one.

### Attack 4 — `JevCallRecord` field-order collision with Design 04

**Flaw.** 02 §2.3 appends `staleness: str = "fresh"` to the frozen
`JevCallRecord`. 04 §2.3 appends `gated_questions`, `unknown_confidence_questions`,
`gated`, `confidence_absent`, `fallback_reason` to the same dataclass. Both
are "appended with defaults — backward compatible," but both claim to be
field 7. If both land, positional construction
`JevCallRecord(s, l, i, o, e, sch, X)` binds `X` differently depending on
merge order, and 04's claim "existing positional constructions keep working"
is only true for 6-arg constructions.

**Why it matters.** 05 §2.6 acceptance criterion #1 requires `JevSensor` to
translate `JudgmentCallRecord` back to `JevCallRecord` "field-for-field" —
the translation surface was specced against the 6-field record. Two
independent appends also break 06's `JevDecisionInfo` expectations only
mildly, but the real cost is process: two designs editing one frozen
dataclass without a shared field-order contract.

**Fix.** Single ownership: one design owns `JevCallRecord` evolution (04
touches it more deeply — give it to 04), and 02 records staleness band via
the existing `stale_cache_hit` bool plus its own trace payload (§5e),
*or* the two designers agree a total field order in writing before either
is implemented. Do not leave "both append" in two docs.

### Attack 5 — Per-key cache write rule vs 04's gated-view write rule

**Flaw.** 02 §3.4: "A re-judge refreshes **all** keys it answers" and
"Keys that disappear from the wrapped output keep their old value under the
ladder until DEAD." 04 §3.3: "On a **successful** call the cache stores the
**gated view** (passing questions only)" — i.e., whole-cache *replace*
with gated-out keys dropped immediately.

**Why it matters.** Concrete scenario: `threat` (Noul 0.51, threshold 0.6)
gates on call N, passes on call N+1. Under 02's per-key rule, call N leaves
the old `threat` value cached and aging (it "disappeared from the output"
only in the gated sense — the model *did* answer). Under 04's rule, call N
stores a cache *without* `threat`. On the failure path at call N+2, does the
stale path serve the aging pre-gate value (02's per-key semantics) or
nothing (04's replaced-view semantics)? 04 §5(b) promises "the stale path
replays only judgments that passed the gate when cached" and "a stale cached
judgment that 'would also gate' today cannot exist." Under per-key merge,
the served value passed the gate *when first cached* (call N−1), so the
letter of the promise holds — but 04's authors clearly wrote §5(b) against a
whole-cache mental model, and 04's test plan asserts "a subsequent forced
error replays `{}` from cache, not older values" for the all-gated case —
which per-key merge violates if *any* key ever passed before (the old value
is still there, aging). The two cache-write rules need one reconciled
specification, not two.

**Question.** Is a gated-out key's previous cached value (a) dropped
immediately (04's replace), (b) kept and aged under the ladder (02's
per-key), or (c) dropped only when the key was gated on the *current* call?
Pick one; the current docs pick (a) and (b) simultaneously.

### Attack 6 — `aupdate_state` overcorrects: sync sensors become unusable

**Flaw.** §2.6: "Sensors that only implement sync `sense()` are **not**
silently wrapped in `asyncio.to_thread`" — and §6.9: a sync-only sensor on
the async path "raises a clear `TypeError`." So a manager holding one
`AsyncSensor` and one plain `Sensor` cannot use `aupdate_state` at all.

**Why it matters.** The realistic Soulscape mix is exactly this: a fast sync
vision sensor plus a slow async judgment sensor. The design forces an
all-or-nothing choice per manager. But the stated reason for the TypeError
— "implicit thread-hopping would violate the thread-safety contract" —
doesn't apply to *calling* a sync function inline: `await`ing the async
sensors and calling `sense()` directly for sync ones in the same coroutine
involves no thread hop at all. The doc rejected the wrong thing (implicit
`to_thread`, correctly rejected) and threw out the obviously fine thing
(inline sync calls) with it.

**Fix.** `aupdate_state` awaits `asense()` where available and calls
`sense()` inline otherwise; reserve `TypeError` for sensors implementing
neither. Keep `to_async` explicit for hosts that want the thread hop.

### Attack 7 — DEAD keys: take the side — keep 0.5.0 retention, and §7.1 is already answered by §1

**Flaw.** §3.6.1 / §7.1 leave open "should the manager unset DEAD keys,"
"leaning keep-as-is."

**Why it matters — and the side.** Take **keep-as-is**, and close the
question, because the doc already decided it: §1 non-goal 3 rejects "a
'smart' cache that silently drops keys the planner depends on" as "a second,
hidden planner." Manager-side unsetting of DEAD keys *is* silently dropping
keys the planner depends on — the exact thing the non-goal forbids. It would
also contradict 04's non-goal 5 ("deleting keys… would surprise every
downstream consumer") and change 0.5.0 behavior the doc promises to preserve.
The honest cost, which the doc should state plainly instead of hedging: a
DEAD key is **indistinguishable from FRESH in the WorldState** — the planner
will act on a value the design declares unusable, with the only signal in
the side channel (Attack 2). Mitigation within the doc's own principles:
treat the STALE band as the action point (hosts replan/verify on STALE so
keys rarely reach DEAD), document DEAD-retention as a known hazard, and
point hosts at `health().dead_keys`. Do not leave §7.1 open — an open
question whose answer is entailed by the doc's own non-goals is indecision,
not prudence.

### Attack 8 — `per_key` overrides: typo-silent config violates fail-loud

**Flaw.** `CachePolicy.per_key` maps key → `(stale_after, max_stale)`, but a
sensor's output keys are unknowable at construction, so `per_key` entries
for keys that never arrive are silently ignored. `per_key={"enemy_distnace":
(2.0, 10.0)}` (typo) does nothing, loudly failing nowhere.

**Why it matters.** The codebase's philosophy is fail-loud on misconfiguration
(`mapping` unknown keys raise `TypeSafeError` at construction — 04 §2.1 even
extends this to `confidence_thresholds`). A safety-critical TTL that silently
doesn't apply is worse than no TTL: the operator believes `enemy_distance`
dies in 10s while it actually lives 30s.

**Fix.** Track per-key-override usage and surface unused entries in
`SensorHealth` (e.g. `unused_policy_keys: tuple[str, ...]`) or log a warning
on first `health()`/`last_report()` after N senses with an override never
matched. At minimum, document the hazard. Better: cut `per_key` (see
Attack 12) — per-key *aging* already solves the "one slow question" problem
on the failure path; per-key *policy* is the marginal addition carrying this
hazard.

### Attack 9 — `ValueMeta.age_seconds` decays at read time; transition-diffing must not use it

**Flaw.** `age_seconds` is computed when the report is built. §5(b)
recommends the interruption worker "diffs consecutive reports' `meta` to
detect FRESH→STALE and STALE→DEAD transitions." A `last_report()` held across
ticks carries frozen `age_seconds` values — diffing them across reports
compares ages computed at different times.

**Why it matters.** Concrete: report at tick 10 says `enemy_distance`
`age_seconds=4.9, staleness=FRESH`; at tick 11 (1s later, no new sense) the
*new* report says `age_seconds=5.9, staleness=STALE`. A naive differ sees a
1.0s age delta and might double-count or mis-order transitions; worse, if
the worker caches `last_report()` and compares a stale object's
`age_seconds` against a fresh threshold, the comparison is against
report-build time, not now.

**Fix.** Spec the transition contract on `(staleness, as_of_monotonic)` —
both monotonic-safe — and document `age_seconds` as "valid at report-build
time only; recompute as `now - as_of_monotonic` for decisions." One sentence
in §2.1.

### Attack 10 — Backward-compat proof is asserted, not constructed

**Flaw.** §2.3/§2.5 promise "`JevSensor` with no new arguments behaves exactly
as 0.5.0" while §5(d) simultaneously proposes refactoring JevSensor's
`min_interval`/`resense_on_change`/`max_stale` logic "onto the per-key ladder
**only if** that refactor is behavior-identical under default arguments (it
must be — 100% coverage plus the unchanged existing test suite is the
proof)."

**Why it matters.** "The unchanged existing test suite is the proof" is not
a proof — the existing suite was written against whole-cache behavior and
cannot detect per-key refactor drift it wasn't designed to catch (e.g. the
gated-view interaction in Attack 5, or timestamp granularity: per-key
`as_of` recorded per key vs one `_last_success` — under `stale_after=None`
these coincide only because re-judge refreshes atomically; any future
partial refresh breaks the invariant silently). The doc makes the compat
claim unconditional in §2.3 ("behaves exactly as 0.5.0") and conditional in
§5(d) ("only if behavior-identical").

**Fix.** Make the claim honest: the default-argument *observable* behavior
is pinned by an explicit equivalence test (same randomized observation/failure
script against 0.5.0 semantics and the new implementation, asserting identical
`sense()` outputs and identical `JevCallRecord` streams), not by the legacy
suite alone. Or drop the internal refactor: keep JevSensor's whole-cache
internals byte-identical and implement the ladder *only* in `CachingSensor`
+ the new `stale_after` path, deleting the "refactored onto the per-key
ladder" sentence.

### Attack 11 — 02's async surface vs 05's "no async in Phase 1"

**Flaw.** 05 §1 non-goal: "No async API in Phase 1. Judgment stays
synchronous… async would force an async agent loop or a sync-bridge, both out
of scope." 02 §2.6 adds `AsyncSensor`, `asense`/`asense_detailed`,
`aupdate_state`/`aupdate_state_detailed`, and `to_async`.

**Why it matters.** Who implements `AsyncSensor` in Phase 1? Not a judgment
backend (05 defers `AsyncJudge`). So the async sensor protocol ships with
zero in-tree producers and its semantics (fail-loud parity, no `to_thread`)
are specified against a future it can't see. It is speculative surface —
and 05's §6(c) explicitly notes async judgment would need cancellation
semantics that don't exist yet. 02 builds the consumer side of an interface
whose producer side was deferred by sibling design.

**Fix.** Cut §2.6 from Phase 1 (or mark it explicitly blocked on 05's
`AsyncJudge`). The sync ladder is the deliverable; async is a second design
pass with real producers.

### Attack 12 — Over-engineering: what to cut

Ranked by cut-worthiness:

1. **`staleness_discount` (§5c) — cut.** Its only consumer (04) explicitly
   refused the composition. Dead on arrival.
2. **`AsyncSensor`/`aupdate_state`/`to_async` (§2.6) — cut or defer.**
   Speculative surface with no Phase-1 producer (Attack 11).
3. **`per_key` policy overrides — cut.** Per-key *aging* (timestamps per
   key) already delivers the "one slow question doesn't poison the rest"
   win, which is the goal's actual ask. Per-key *policy* adds a validation
   surface, the typo-silent hazard (Attack 8), and a `CachePolicy`
   constructor nobody can fully validate. Ship per-sensor policy +
   per-key aging; add overrides when a real caller needs them.
4. **`tolerant_fingerprint` helper — cut the helper, keep the hook.**
   The `observation_fingerprint` hook point is the valuable seam; the
   shipped rounding helper bakes in one tolerance semantic (and its NaN
   behavior — NaN leaves never compare equal, so NaN-containing
   observations re-sense forever, silently defeating `min_interval`).
   Hosts with float noise can write ten lines; the library shouldn't bless
   one.
5. **Keep:** the ladder bands, `CachingSensor`, `health()` aggregation
   (minus the `known`-flag bikeshed — just use `status` + nullable
   fields), `sense_detailed()` *if* Attack 2 is answered with a real
   consumer, monotonic-clock aging, the `stale_after=None` default.

### Attack 13 — Minor: sketch code fails the project's own lint

**Flaw.** §2.1 sketches `from typing import Any, Callable, Protocol`. Ruff
selects `UP` (pyupgrade); `typing.Callable`/`typing.Protocol` imports trigger
UP035 — the codebase imports both from `collections.abc` (see `jev.py`).
`dataclass` field ordering in `CachePolicy` (defaults after non-defaults is
fine here) is OK, but `StalePolicy`/`CachePolicy`/`SensorHealth` defined in
`caching.py` while `SensorHealth.health()` is referenced in §2.2 before its
§2.5 definition — presentation only.

**Why it matters.** Small, but the design will be implemented by copying the
sketches; sketches that fail `ruff check`/`mypy` waste the implementer's
first pass. Also unlisted: the new `__all__` entries are never enumerated
(the constraint requires `goapauto/__init__.py` `__all__` updates —
`CachingSensor`, `CachePolicy`, `StalePolicy`, `Staleness`, `ValueMeta`,
`SensorReport`, `SensorHealth`, `staleness_discount`,
`tolerant_fingerprint`, `to_async`, and a decision on `AsyncSensor`).

**Fix.** Fix the imports in the sketch; add the explicit `__all__` delta.

### Attack 14 — `min_interval` vs `stale_after` footgun is documented but unguarded

**Flaw.** §3.6.2 correctly notes `min_interval=60, stale_after=5` "will
routinely serve STALE data between re-queries — that is the operator's
explicit choice." It is, but it's also the *default-adjacent* trap:
`min_interval` throttles re-queries while the ladder flags cache age, and
nothing relates the two knobs. Worse, with 0.5.0 defaults
(`min_interval=0.0`, `resense_on_change=True`), `_should_resense` re-judges
on *every* `sense()` for unchanged observations (the interval check
`(now - last_call) >= 0.0` is always true — 05 §3 documents this quirk),
so the ladder's FRESH/STALE bands are nearly unreachable on the success
path; the ladder only matters on the failure path. The design never states
this plainly.

**Why it matters.** An operator setting `stale_after=5` expecting a
degradation ladder in normal operation gets one only when the backend is
*failing*. In the common case the sensor re-queries every tick (paying
292ms p50 per the bench notes) and the ladder is decorative. The doc's
§2.7 example (`stale_after=5.0` on a vision sensor) implies gradual
degradation during normal throttled operation, which requires
`min_interval > 0` to actually happen.

**Question.** Should construction warn when `stale_after < min_interval`
(the ladder's STALE band is unreachable-by-design between re-queries), and
should the doc state up front that with default `min_interval=0.0` the
ladder is failure-path-only?

---

## Constraint-violation check

| Constraint | Verdict | Note |
|---|---|---|
| uv workflow | **PASS** | Design-only; test plan uses `pytest`, no pip. |
| Python ≥ 3.12 | **PASS** | Sketches use 3.10+ syntax throughout; no 3.13-only constructs. |
| 100% coverage, no `pragma: no cover` | **PASS (conditional)** | §6 test plan asserts it; but the plan's branch list doesn't cover the 02/04 merge-order or per-key-vs-gated-view interactions (Attacks 4, 5) — coverage of *specified* branches, not of *omitted* ones. |
| ruff + mypy green | **FAIL (minor)** | Sketch imports `Callable`, `Protocol` from `typing` — UP035 violation against the repo's `collections.abc` convention (Attack 13). Sketch-level, fixable. |
| No new required dependencies | **PASS** | stdlib only (`dataclasses`, `enum`, `time`, `asyncio`, `logging`). |
| Opt-in / backward compatible, defaults unchanged | **FAIL (structural)** | The default-behavior claim is asserted while §5(d) makes the internal per-key refactor conditional on behavior-identity it doesn't prove (Attack 10); and the doc shares `JevCallRecord` evolution with 04 with no field-order contract (Attack 4). The *intent* is compatible; the *guarantee* is not constructed. |
| Public API via `goapauto/__init__.py` `__all__` | **FAIL (minor)** | §7.4 gestures at `__all__` but never enumerates the new exports; `AsyncSensor` export status undecided (Attack 13). |

---

## Interaction-consistency check (doc by doc)

### 01 — Runtime / replan budgets: FRICTION (no hard contradiction)
- **Agreement:** 01 §5(a) keeps the seam provider-free (`sensor_age: float`,
  never Jev types); 02 doesn't ask 01 to import sensor types. Both treat
  sensing and planning as separate layers.
- **Friction:** 02 produces per-key staleness bands; 01 consumes a single
  scalar `sensor_age`. 01 §5(a) still speaks of "when JevSensor degrades to
  `{}` (older than `max_stale`)" — under 02's ladder the sensor degrades
  *gradually* (STALE served, flagged), reaching `{}` only past `max_stale`.
  A scalar can't express "3 keys FRESH, 1 STALE, 1 DEAD," so 01's
  `SENSORS_STALE` gate is all-or-nothing on information 02 worked to make
  granular. 02's §5(a) proposes `max_age()`/`oldest_staleness()` helpers;
  01 never acknowledges them. Somebody's vocabulary has to bend.

### 03 — Action interruption: GAP (workable, but the seam doesn't fit the recommendation)
- **Agreement:** 03 §5(b) assigns world-change *definition* to 02 and owns
  the boundary; 02 §5(b) declines to emit events. Clean split in principle.
- **Gap:** 02's recommended contract — "the interruption checker calls
  `sensor.last_report()` after each `update_state_detailed` and diffs
  metas" — doesn't fit 03's seam `interrupt_when(state, next_action)`,
  which receives no sensor reference; the host must close over it. Workable
  but un-specified. Drive-by: 03's own example reads
  `world_state.generation`, an attribute that does not exist on 0.5.0
  `WorldState` (verified — no `generation` in `worldstate.py`); 03's pull
  wiring example is therefore fictional until something provides the
  counter, and 02 doesn't provide it either.

### 04 — Confidence gating: HARD CONTRADICTION (fail)
- **Contradiction 1:** staleness×confidence composition — 02 §5(c)
  multiplicative linear decay vs 04 §5(b) conjunction, no decay. Mutually
  exclusive; see Attack 1.
- **Contradiction 2 (latent):** cache write rule — 02 per-key merge
  (§3.4) vs 04 whole-gated-view replace (§3.3). See Attack 5.
- **Collision:** both append fields to the frozen `JevCallRecord` with no
  shared field order. See Attack 4.
- **Agreement:** both keep `sense() -> dict` and 0.5.0 defaults; 04's
  "gate at write, replay at read" is at least *stated* as the composition
  rule — which is exactly why 02's competing rule has to go.

### 05 — Provider-independent judgment: AGREEMENT with an ordering hazard
- **Agreement:** 05 §5/§6(b) "caching lives in `JudgmentSensor`, above the
  protocol; the protocol stays a pure function of (state, questions)" —
  02 §5(d) says the same thing ("Caching lives in a layer above the judge
  protocol… caching wraps the protocol, never implements it"). Genuine
  alignment, the strongest cross-doc agreement in the set.
- **Hazard:** 05 plans `JevSensor` → internal delegation to
  `JudgmentSensor`+`JevJudge` (Phase 2) precisely to avoid two caching
  implementations drifting. But 02 §5(d) says "`JevSensor` adopts the
  policy types [`CachePolicy`, per-key ladder] for its own cache rather
  than growing a parallel implementation." If 02 lands a `CachePolicy`
  ladder inside `JevSensor` and 05 later delegates `JevSensor` to a
  `JudgmentSensor` specified with plain `max_stale`, there are two caching
  implementations — the exact fork 05 §2.6 warns against. 02 should be
  re-targeted: specify the ladder against `JudgmentSensor` (05's generic
  layer), with `JevSensor` getting a thin parameter translation, or the
  two designers agree which layer owns `CachePolicy`.
- **Friction:** 05 defers async judgment ("No async API in Phase 1"); 02
  ships `AsyncSensor`/`aupdate_state` with no producer. See Attack 11.

### 06 — Why-diagnostics: VOCABULARY MISMATCH (fail on details, agree on direction)
- **Agreement:** both want data age in the trace; 02 §5(e) offers a payload
  table "so their schema can reserve the fields" and defers format
  ownership to 06. Right instinct.
- **Mismatch 1:** 06 §6(b) consumes staleness via a duck-typed
  `diagnostics() -> Mapping[str, Any]` merged into `sensor_update`
  (`data_age_ms`, `cache="hit"|"miss"|"stale"`). 02 provides `health() ->
  list[SensorHealth]` and `last_report() -> SensorReport` — neither is a
  `diagnostics()` mapping. Under 02-as-written, 06's `data_age_ms` is
  *always* `None` ("sensor does not report data age") and the per-key ages
  02 computed are unreachable through the seam 06 specified.
- **Mismatch 2:** 06's `sensor_stale` event is `{sensor, age_ms,
  max_stale_ms, keys}` — one age per sensor. 02's reality is per-key ages
  and per-key bands. 06 cannot faithfully fill `age_ms` under per-key
  staleness; the schema assumes the whole-cache model 02 abolishes.
- **Mismatch 3:** 06 §6(b) derives `stale_served` from `JevStats`
  deltas (`stale_cache_hits`), which 02 leaves unchanged — fine — but 02's
  STALE *band* (served-by-policy, not on the failure path) never touches
  `stale_cache_hits`, so 06's `sensor_stale` event fires only for
  failure-path reuse, never for ladder-band STALE service. Half the ladder
  is invisible to the inspector.
- Net: 06 was written against 0.5.0's whole-cache JevSensor; 02 changes the
  model underneath it without updating 06's consumption contract.

---

## What they chose NOT to build — the background-thread question, argued both sides

**For rejection (the doc's side, steelmanned):** The library's
not-thread-safe contract is load-bearing — `SensorManager`, `JevSensor`,
and the planner all document per-thread ownership. A background refresher
needs locking discipline across the cache, the WorldState, and the planner
loop; getting it wrong produces Heisenbugs in game loops, the worst kind.
`min_interval` + the ladder already bound worst-case tick latency: with
`min_interval=1.0` a 292ms judgment costs one slow tick per second, and the
STALE flag makes the staleness visible. The host *can* schedule refreshes
on its own loop between ticks. For a library, "no threads" is a defensible,
principled boundary.

**Against rejection (the hostile side):** A 292ms p50 / 330ms p95 judgment
(measured, Stage B) on a 16ms tick means every `min_interval` refresh blows
~18–20 consecutive ticks *synchronously* — `sense()` is called inline by
`update_state`, so there is no way to keep the tick budget without either
(a) threads, (b) an async seam with a real producer (deferred by 05), or
(c) never re-judging during gameplay (then why have the ladder?). The doc's
answer — host-side prefetch via `judge()` on another thread — is both
racy (Attack 3) and not actually prefetch (judge takes observations, not
answers). So the rejection leaves the flagship sensor (JevSensor, the only
292ms sensor in the tree) with no viable low-latency story: the ladder
makes slow judgments *visible* but the architecture still *pays* for them
inline. If "no threads" is truly the principle, the doc owes a worked
16ms-tick example showing where the 292ms goes. It doesn't have one.

**My call:** the rejection is tenable *only* if the doc stops pretending
the host pattern covers it. Either scope the design to "sensors whose
`sense()` is tick-cheap" and say JevSensor-at-16ms needs 05's future
`AsyncJudge`, or admit Phase 1 has no answer for slow judgments on fast
ticks. What it can't do is reject threads *and* bless a racy workaround.

---

## Summary of required changes (for NEEDS-REWORK → APPROVE-WITH-CHANGES)

1. Resolve the 02/04 composition contradiction: delete `staleness_discount`
   from 02 (04's conjunction wins — it even argues against the cliff the
   decay reintroduces).
2. Reconcile the cache write rule: one spec for what a re-judge (with
   gating enabled) writes — per-key merge and whole-gated-view replace
   cannot coexist.
3. Establish the shared `JevCallRecord` field order with 04 (or single-owner
   the dataclass), before either is implemented.
4. Answer Attack 2 with a named in-core consumer of `SensorReport`, or cut
   the parallel channel to `health()`.
5. Retract or repair the sanctioned cross-thread `judge()` pattern; state
   the actual Phase-1 story for 292ms judgments on fast ticks.
6. Close §7.1 (keep 0.5.0 DEAD retention — the doc's own non-goal 3 already
   decides it) and reconcile the 06 consumption vocabulary
   (`diagnostics()` vs `health()`/`last_report()`; per-key ages vs
   `sensor_stale.age_ms`).
7. Re-target the ladder at 05's `JudgmentSensor` (or get 05's sign-off that
   `CachePolicy` lives in `JevSensor` without forking the cache
   implementation 05 was created to prevent); defer §2.6 async to 05's
   `AsyncJudge`.

---

*Review produced read-only. No source files modified, no git state changed,
`goapauto-proto/` and `goapauto-bench/` untouched.*
