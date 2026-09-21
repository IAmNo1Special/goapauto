# Red-Team Review — 05 Provider-Independent Judgment Interface

**Target:** `designs/05-provider-independent-judgment.md` (2026-09-21, Phase 1 design)
**Reviewer role:** hostile. Checked against `src/goapauto/models/jev.py` (483 lines),
`src/goapauto/__init__.py`, `src/goapauto/testing/`, `tests/goapauto/models/test_jev.py`,
and the interaction sections of docs 01, 02, 03, 04, 06.
**Read-only review:** no source files modified.

## Verdict: APPROVE-WITH-CHANGES

The skeleton is sound — `Judge` protocol, `JevJudge` adapter, caching above the
protocol, no new backends, sync-only for Phase 1. But the doc contains one
**internal contradiction that makes the failure table unimplementable as written**
(#1), two **blocking cross-doc contradictions** on confidence policy (#2, #3),
and a **Phase 2 delegation plan whose acceptance criteria are provably insufficient**
(#4, #5). None of these require redesigning the interface; all of them require
decisions before Phase 1 code is written, because they determine the adapter's
extraction code, the sensor's except-clause structure, and the telemetry record
shapes. Ship the protocol shape; do not ship the contradictions.

## The three strongest attacks

1. **The failure table contradicts §3 and the pinned tests on contract violations.**
   §4 says a backend that drops a question or returns a wrong-typed answer makes
   `sense()` *propagate* `JudgmentError`; §3 says *all* `JudgmentError`s go through
   the stale-cache path; `test_missing_answer` and `test_garbled_answer_degrades_gracefully`
   in `tests/goapauto/models/test_jev.py` *require* absorption (`sense() == {}` /
   cache reuse). All three cannot hold — and both rows are `JudgmentError(retryable=False)`,
   so no discriminator exists to implement the table.
2. **Confidence policy contradicts doc 04 on two test-pinned points.** Doc 04 raises
   `TypeSafeError` on out-of-range confidence (fail-loud; NaN/1.5 → raise is in its
   test plan) and derives Noul decisiveness `2·|noul−0.5|`; doc 05 says adapters MUST
   clamp and maps Noul/Score confidence to `None` when absent. Same backend, opposite
   behavior — this decides the adapter's extraction code and cannot be deferred.
3. **Phase 2 delegation is unachievable as specified.** The Score legend→interval→legend
   round-trip is lossy (the doc punts it to open question Q3 yet still says Phase 2
   SHOULD delegate); `JevGoalStrategy`'s dict-valued criteria must survive a
   `Mapping[str, str]` JSON-string round-trip that changes the model prompt; and
   `test_strategy_records_failure` pins `records[0].error == "TypeSafeError"`, which
   naive delegation breaks (`"JudgmentError"`). The "unchanged test suite as regression
   net" cannot catch prompt drift — no test pins the exact SDK questions forwarded
   to `system_one`.

---

## Numbered attacks

### 1. Internal contradiction: do contract violations propagate or get absorbed?

**The flaw.** Three mutually exclusive claims:
- §3 (`JudgmentSensor.sense()` semantics): "On `JudgmentError` during re-judge:
  reuse `self._cached` iff fresh… else return `{}`." All `JudgmentError`s absorbed.
- §4 failure table: "Backend drops a question (missing key)" and "Backend returns
  wrong answer types" → `JudgmentError(retryable=False)`, "propagates out of
  `sense()` (NOT absorbed by the stale path)".
- The pinned 0.5.0 tests: `test_missing_answer` (`answers={}` → `sense() == {}`,
  `"Missing answer"` in caplog) and `test_garbled_answer_degrades_gracefully`
  (garbled answer → "reusing last judgments", cache reused) **require absorption**.

There is no discriminator: both the outage case and the contract-violation case are
`JudgmentError(retryable=False)`. The sensor's except-clause cannot implement the table.

**Why it matters (concrete).** `JevJudge` wrapping `FakeTypeSafeClient` with no canned
answer for `"danger"` raises `JudgmentError(retryable=False, "Missing answer…")`.
Under §3 the generic sensor absorbs it into the stale path (test passes). Under §4 it
must propagate (test fails). The implementer has to pick, and whichever they pick
contradicts a normative section of the doc. §2.8's NOTE ("`JudgmentError` from
`sense()` therefore means a contract violation") then describes an impossible
situation under the absorbed model — the example's try/except is dead code either way.

**Fix or question.** The tests decide: **absorb**. Delete the two "propagates" rows from
the §4 table, or — if the designer truly wants violations to propagate — introduce a
real discriminator (e.g. a `JudgmentContractError(JudgmentError)` subtype that bypasses
the stale path) and accept that `test_missing_answer` / `test_garbled_answer_degrades_gracefully`
must then change, which violates the frozen surface. Sharp question: *which section is
wrong, §3 or §4?* They cannot both ship.

### 2. Clamp-vs-raise: doc 05 contradicts doc 04 on out-of-range confidence (BLOCKING)

**The flaw.** Doc 05 §2.2/§3.5: "Adapters MUST clamp out-of-range values (or map them);
the protocol guarantees the range, so gating layers never see `1.7` or `-0.2`" and
"the generic layer TRUSTS the range (no double-clamping — clamping twice hides
adapter bugs; instead, adapters are unit-tested on clamping)". Doc 04 §5(d) and §7
open question 2: "This design raises `TypeSafeError` (fail-loud). Clamping into
`[0,1]` would be more forgiving of sloppy third-party backends" — and doc 04's test
plan pins "`NaN` and `1.5` reported confidence → `TypeSafeError`".

**Why it matters (concrete).** A `FakeTypeSafeClient` responder returns
`ChoiceAnswer(choice="x", confidence=1.5, …)`. Doc 04's `JevSensor` raises
`TypeSafeError`. Doc 05's `JevJudge` clamps to `1.0` and proceeds. Same backend,
opposite behavior — and the adapter's extraction code cannot be written until this
is decided. Note the doc's reasoning conflates two things: *clamping* hides adapter
bugs, but *validating-and-raising* does not hide anything — a range check that raises
is fail-loud, exactly what doc 04 wants. "Trust without re-validating" is the wrong
inference from "don't clamp twice".

**Fix.** Validate-and-raise in the generic layer (cheap, fail-loud, no hiding), error
type `JudgmentError` (generic layers cannot import SDK types — which also means doc 04's
`TypeSafeError` must become `JudgmentError` wherever the generic path is involved; see
interaction check). Delete "the protocol guarantees the range" as an adapter-enforced
postcondition; restate as "adapters SHOULD normalize; generic layers VERIFY."

### 3. Jev Noul/Score confidence: derived decisiveness vs `None` (BLOCKING)

**The flaw.** Doc 04 §5(d): "The Jev adapter always produces a value (model-reported
for Choice/Score, derived decisiveness for Noul), so Jev-backed callers never hit the
`None` path." Its test plan pins the derivation: Noul `0.5 → 0.0`, `0.0/1.0 → 1.0`,
`0.75 → 0.5`. Doc 05 §2.5: "read confidence where the SDK provides it (`ChoiceAnswer`
carries confidence; Noul/Score answers map to `None` when absent)."

**Why it matters (concrete).** The SDK *does* provide Score confidence
(`tests/goapauto/models/test_jev.py` builds `ScoreAnswer(…, confidence=0.8, …)`), so
doc 05's "map to `None` when absent" is misleading for the real Jev backend — and for
Noul the two docs genuinely disagree: `NoulAnswer(noul=0.75)` with no reported
confidence becomes decisiveness `0.5` under doc 04 (gated against the threshold) vs
`None` under doc 05 (pass-through + flagged, or treat-as-1.0 — see #4 below, the docs
disagree there too). Doc 04's own open question 1 ("Is `2·|noul−0.5|` the right proxy,
or should Noul answers always pass the gate? …do not guess") admits the derivation is
unvalidated. An uncalibrated invention cannot be the pinned behavior of the reference
adapter.

**Fix.** Decide once, in one doc: use SDK-reported confidence when present, `None`
when absent; drop the derived decisiveness (doc 04's open Q1 already questions it).
Then reconcile the absent-confidence policy (attack #4).

### 4. Absent-confidence policy: treat-as-1.0 vs pass-through+flagged

**The flaw.** Doc 05 §6(d) "recommends" treat-absent-as-1.0 ("a backend that cannot
report confidence is trusted at face value"). Doc 04 §5(d): "`None` → pass-through +
flagged (§3.2), never gated, never coerced to `0.0`" — with telemetry fields
`unknown_confidence_questions` / `confidence_absent` recording the pass-through.

**Why it matters.** Outcome-adjacent but telemetry-divergent: under doc 05's
recommendation a `FakeJudge` (confidence `None` by default, §6(e)) sails through
gating silently; under doc 04 every such call is flagged. Worse, doc 05's rationale
("the alternative treat-absent-as-0.0 would make every non-probabilistic backend fail
every gate") argues against a strawman — doc 04 never proposes 0.0. The live question
is whether absent confidence is *flagged*, and the two docs give different answers.

**Fix.** Adopt doc 04's pass-through + flagged (it preserves information; treat-as-1.0
destroys the distinction between "confident" and "unknown"), and say so normatively
in doc 05 instead of "recommending" the opposite.

### 5. Phase 2 delegation acceptance criteria are insufficient (telemetry VALUES, not just types)

**The flaw.** Criterion #1 says "`JevCallRecord` must keep its exact fields (frozen).
`JudgmentSensor` emits `JudgmentCallRecord` (a superset with `backend`);
`JevSensor` translates back to `JevCallRecord` field-for-field." But the pinned failure
is in *values*: `test_strategy_records_failure` asserts
`records[0].error == "TypeSafeError"`. Under delegation the inner generic strategy
records `error="JudgmentError"` (it only knows the translated exception). "Field-for-field"
translation preserves the field but not the value — the test fails unless the outer
layer rewrites `"JudgmentError"` → `"TypeSafeError"` via the chained `__cause__`,
which is value-rewriting, not field translation, and is nowhere specified. Same for
the sensor's failure path (`test_sensor_records_failure` pins `error == "TypeSafeError"`,
line ~722).

Compounding it: docs 02 and 04 *also* extend `JevCallRecord`/`JevStats` (`staleness:
str = "fresh"` in doc 02 §5(e); `gated_questions`, `unknown_confidence_questions`,
`gated`, `confidence_absent`, `fallback_reason`, `JevStats.gated` in doc 04 §2.3).
Doc 05's `JudgmentCallRecord` (source, latency_ms, input/output tokens, error,
stale_cache_hit, backend) omits every one of them — the "superset" claim is already
false, and Phase-2 translation would silently drop staleness and gating telemetry on
the frozen surface.

**Why it matters (concrete).** After delegation, a Soulscape operator's existing
telemetry pipeline reading `record.error` sees `"JudgmentError"` where it saw
`"TypeSafeError"`, and the `staleness`/`gated_questions` fields doc 02/04 promised
vanish for Jev-backed sensors. That is observable behavior drift on the frozen surface.

**Fix.** (a) Specify the error-name translation rule explicitly (e.g. outer layer
reports `type(exc.__cause__).__name__` when the cause is a backend-native error, else
`type(exc).__name__` — and pin it in a new characterization test). (b) Define
`JudgmentCallRecord`/`JudgmentStats` as a true superset of the *final* `JevCallRecord`/
`JevStats` after docs 02+04 land, or state the merge order explicitly. (c) Add the
missing characterization tests (see #6).

### 6. The "unchanged test suite as regression net" cannot catch prompt drift — delegation needs NEW characterization tests

**The flaw.** The lossy translations in the delegation path change what the model
*sees*, and no existing test observes that:
- **Score legend round-trip.** `JevSensor(questions={"hunger": Score(instructions=…,
  criteria=["not hungry", "hungry", "starving"])})` (real shape from
  `test_jev.py::sdk_questions`) → core `ScoreQuestion(min_value=?, max_value=?,
  rubric=?)` → `JevJudge` rebuilds `Score(legend=?)`. What min/max does a criteria
  *list* map to? How many legend rungs does the adapter derive, with what labels?
  Doc 05 §8 Q3 admits this is unresolved ("should be reviewed against real Jev
  behavior in Phase 2, not fixed in this design") — yet §2.6 says Phase 2 SHOULD
  delegate. Delegation is **BLOCKED** on Q3, not merely "should".
- **Choice criteria rendering.** `JevGoalStrategy` builds
  `criteria={label: {"target_state": …, "priority": …}}` — dict *values*. Core
  `ChoiceQuestion.descriptions` is `Mapping[str, str]`. The doc JSON-dumps the dicts
  to strings and lets "the adapter decide rendering" — the model then sees
  `'{"target_state": …}'` strings instead of structured criteria. Prompt change,
  zero test coverage (`FakeTypeSafeClient.calls` records received questions but no
  test asserts on them).
- **Constructor ordering / client leak.** `JevSensor.__init__` today validates
  *before* creating the client. Under delegation, `JevSensor.__init__` must
  translate questions → construct `JevJudge` (which may create a `TypeSafeClient`) →
  construct `JudgmentSensor` (which validates). If `JudgmentSensor.__init__` raises
  (bad mapping), who closes the already-created client? Unspecified.
- **Double-close idempotency** is claimed ("Idempotent") but untested anywhere.
- **Telemetry on contract violation:** does `_calls` increment when a
  contract-violation `JudgmentError` propagates (attack #1's other branch)? The
  current code records telemetry only inside `except TypeSafeError`; the generic
  version's behavior here is unspecified, so `stats()` can drift.

**Fix.** New characterization tests, written *before* Phase 2, pinning: exact SDK
question objects forwarded to `system_one` (via `FakeTypeSafeClient.calls`) for a
representative Noul/Choice/Score set incl. `JevGoalStrategy`'s dict-valued criteria;
exact exception type+message+chain on construction failures; client-close behavior
when construction fails mid-way; double `close()`; stats counter behavior on every
failure-table row. Mark delegation BLOCKED on Q3 + the criteria-rendering decision.

### 7. `retryable` is the worst of both worlds — and §2.5 contradicts §4 about it

**The flaw.** Nothing consumes `retryable` (no auto-retry by design — "retry is the
caller/agent-loop's decision"). A boolean cannot distinguish rate-limit (retry after
backoff) from auth failure (never retry; fix config) from contract violation (fix
code). And §2.5 honestly admits the SDK "does not currently expose a
machine-readable transient/permanent distinction, so the adapter defaults
`retryable=True` except for construction-time validation failures" — while §4's table
claims "Backend auth/config error (bad key) → `JudgmentError(retryable=False)`".
A *runtime* auth failure (revoked key — the common case) gets `retryable=True`.

**Why it matters (concrete).** 3am: TypeSafe key revoked. Logs say
`JudgmentError(backend="jev", retryable=True)`. The operator (or doc 06's
why-trace) reads "transient blip, wait it out" when the truth is "fix config".
The flag doesn't just fail to help — it actively misleads, and §4 documents the
opposite of what the code does.

**Fix.** Cut the field: nothing reads it, and the exception class name is already in
telemetry (`record.error`). If a machine-readable signal is truly wanted later, it
wants to be a string category (`"transient" | "config" | "contract"`), not a bool.
At minimum, fix the §2.5/§4 contradiction and stop claiming auth errors map to
`retryable=False`.

### 8. Non-`JudgmentError` propagation: "fail loudly on bugs" conflates adapter omissions with transport reality

**The flaw.** §2.4/§3.7: a `Judge` leaking a backend-native exception (e.g. raw
`httpx.TimeoutError`) is "a backend bug; generic layers let it propagate so the bug
is visible." But `httpx.TimeoutError` from a correctly-written adapter is not a
programmer bug — it is a transport error the adapter *failed to translate*, i.e. an
adapter omission. The cost is total: it skips the `except JudgmentError` stale-cache
path (no graceful degradation) AND skips `_record_telemetry` (stats undercount —
`calls`/`errors` diverge from reality), and kills the agent loop. The doc sells
inherited fragility (`JevSensor` today also only catches `TypeSafeError`) as a
designed virtue.

**Why it matters (concrete).** `typesafe_sdk` raises an unwrapped `httpx.ConnectError`
(a real SDK behavior pattern — not every transport failure arrives as
`TypeSafeError`). `JevJudge` translates only `TypeSafeError`. The raw `ConnectError`
propagates through `JudgmentSensor.sense()` → the Soulscape tick dies → no stale
reuse, no telemetry. The "declared outage" path the doc is proud of never fires for
exactly the failures it was built for.

**Fix.** Make translation completeness part of the adapter contract: adapters MUST
translate all transport exceptions (pin with a misbehaving-client test in the adapter
suite), and generic layers wrap *unknown* exceptions as
`JudgmentError(retryable=True)` **with chaining** (`from exc`). Chaining preserves
loudness for real bugs (the traceback still shows the `AttributeError`) while keeping
the loop alive. Loud AND resilient beats loud OR resilient.

### 9. The closed `Question` union is Jev lock-in with extra steps

**The flaw.** `Question = NoulQuestion | ChoiceQuestion | ScoreQuestion`, and
`JudgmentSensor` raises `TypeError` for unknown question types at construction. A
third-party backend with a fourth primitive (ranked list, free-form JSON schema,
multi-label) **cannot** be used with `JudgmentSensor` — it requires a core change.
The doc is honest about this ("documented as Jev-shaped… must justify an interface
revision") but then "provider-independent" means "Jev-shaped until core is revised",
which is lock-in with a disclaimer. The doc's own `InstructorJudge` sketch proves
the strain: Noul is a *graded* truth in [0,1]; the sketch maps it to a `bool` field
then `float(bool)` ∈ {0.0, 1.0} — semantic narrowing presented as "mechanical
translation".

**Why it matters.** The first real non-Jev backend (the doc names Instructor) that
needs something the three primitives can't express has exactly two options:
shoehorn itself in with semantic loss, or wait for a core revision. The interface
fossilizes around Jev at the moment of its creation.

**Fix or question.** Either (a) stop claiming provider-independence — frame it as
"the Jev-shaped judgment seam" (honest, and the design is still valuable); or
(b) add a real escape hatch: `JudgmentSensor` passes unknown question types through
to the judge untouched, and the *adapter* validates/rejects what its backend can't
express (fail-loud at the adapter, which knows its backend). Sharp question the
designer must answer: *name three concrete backends that can be built on exactly
these three primitives with zero semantic loss. If the list is "Jev and toys", the
abstraction is unvalidated* (see #10).

### 10. The single-fake validation is circular — a protocol with one implementation is a hypothesis

**The flaw.** The "provider-independence proof" (§7) is `FakeJudge` returning canned
`Answer` objects — written by the same designer, against the same three primitives,
doing *no translation work*. It proves the generic layers accept the `Answer`
dataclasses, not that the three primitives suffice for a non-Jev backend. The
parametrized suite is weaker still: the `JevJudge(FakeTypeSafeClient(…))` arm is
Jev-shaped end-to-end (core→SDK→core); only the `FakeJudge` arm differs, and it
differs only in that it skips translation entirely.

**Why it matters.** A fake that cannot fail to translate proves nothing about
translatability. The exact failure mode the abstraction must survive — "backend's
native shape doesn't fit the three primitives" — is untestable with a canned-answer
fake.

**Fix.** Require `FakeJudge` to do REAL translation from a deliberately alien
internal representation: e.g. everything-is-a-text-prompt (render each core question
to text, parse the answer back from text, including clamping/parsing failures), or a
rule-based judge that must bin `ScoreQuestion` intervals onto its own discrete scale
and *reject* what it can't express. Include a negative test: a question shape the
fake cannot express must raise `JudgmentError(retryable=False)`. Phase 1 should not
pass without at least one backend that translates rather than echoes.

### 11. Sync-only + not-a-cancellation-point = hung backend hangs the agent, permanently unowned

**The flaw.** §6(c): "a `sense()` call in progress is a plain blocking call like any
other and is not a cancellation point." Every backend is network-bound; the defaults
are 30s timeouts (`TypeSafeClient(timeout=30.0)`). Doc 03 agrees ("No touchpoint"),
so *no design* owns interrupting a hung judgment. Doc 05 §6(a) claims "budgets are
enforced *around* judgment… `JudgmentResponse` carries `latency_ms` (measured by the
adapter) so budget accounting can attribute time precisely" — but `latency_ms` only
exists *after* the call returns; there is no mechanism for the budget layer (doc 01)
to time-box or observe an in-flight `judge()`.

**Why it matters (concrete).** Two `JudgmentSensor`s in a `SensorManager`; backend A
hangs (black-holed connection, 30s timeout). `sense()` blocks 30s+. The replan budget
already blew mid-sense and cannot even observe it. The interruption design — the one
system built for "stop doing that" — explicitly cannot reach it.

**Fix or question.** At minimum, document the consequence normatively ("a hung
judgment backend blocks the sense→plan pipeline for its full timeout; this is
accepted for Phase 1") so doc 01/03 don't assume a hook exists. Better: define a
deadline convention on the protocol now (even sync — e.g. an optional
`deadline: float | None` monotonic timestamp that adapters SHOULD honor by
configuring client timeouts), so a future watchdog has something to enforce. Sharp
question: *is permanently uninter ruptible network I/O in the sense path acceptable
for Soulscape's live loop, or does the "no cancellation point" stance need a timeout
story before this ships?*

### 12. Construction error types are inconsistent

**The flaw.** The §3 table: empty questions → `JudgmentError(retryable=False)`
("ValueError-equivalent; see note"); `max_stale < 0` → `ValueError`; duplicate/empty
choices → `ValueError`; inverted score interval → `ValueError`. All four are
construction-time programmer/config errors; two different exception types for no
principled reason — the note even apologizes for it.

**Why it matters (concrete).** Agent bootstrap code doing `except ValueError` for
"my config is wrong, fail fast" misses the empty-questions case, which arrives as
`JudgmentError` — the same type as a *runtime backend outage*. Fail-fast config
validation and tolerate-outage handling cannot be separated without inspecting
`retryable`, which attack #7 shows is unreliable.

**Fix.** Construction violations → `ValueError` (or a `JudgmentConfigError(ValueError)`
if a distinct type is wanted); reserve `JudgmentError` for runtime backend failures.
This also deletes the "ValueError-equivalent" apology.

### 13. `FakeJudge` is unimportable exactly where it's needed (goapauto.testing import trap)

**The flaw.** §7: "`FakeJudge` — the provider-independence proof (in `goapauto.testing`,
alongside `FakeTypeSafeClient`; it must NOT import `typesafe_sdk`)". But
`src/goapauto/testing/__init__.py` eagerly does
`from goapauto.testing.fake_client import FakeTypeSafeClient`, and `fake_client.py`
raises `ImportError` without the `[jev]` extra. So `from goapauto.testing import
FakeJudge` raises `ImportError` in a no-Jev environment — exactly the environment the
provider-independence story is for. Dev/CI won't catch it: `typesafe-sdk` is in dev
dependencies (`pyproject.toml` line 38).

**Why it matters.** The flagship artifact of the "no SDK" claim can't be imported
without the SDK installed. The "zero SDK imports in the `FakeJudge` parametrization"
test-plan claim is true of the *module* but false of the *package import path* the doc
specifies.

**Fix.** PEP 562 lazy loading in `goapauto/testing/__init__.py` (mirroring the main
package's `_JEV_ATTRS` pattern), or document `goapauto.testing.fake_judge` as the
direct import path. Note coverage `source=["goapauto"]` includes the new module —
100% applies to it too.

### 14. `usage` is untyped and self-contradictory

**The flaw.** §2.3: `usage: Mapping[str, Any] | None` — "Telemetry callbacks receive
the raw mapping; interpretation is the callback's job." §6(e): the *sensor* fills
`JudgmentCallRecord.input_tokens/output_tokens` from that mapping — so the sensor
does the interpretation, against an undocumented key convention.

**Why it matters (concrete).** A third-party adapter reports OpenAI-style
`{"prompt_tokens": N, "completion_tokens": M}`. The sensor looks for
`input_tokens`/`output_tokens`, finds neither, records `None`/`None`. Token
telemetry silently goes dark, and doc 06's trace consumer gets empty fields with no
error. "Backend-defined" + sensor-side interpretation = a contract neither side
fully owns.

**Fix.** Type it: a small frozen dataclass (e.g. `TokenUsage(input_tokens: int |
None, output_tokens: int | None)`) that adapters must populate, or document the
exact key contract (`"input_tokens"`, `"output_tokens"`) as normative for adapters.
Delete "interpretation is the callback's job" or move it to genuinely
backend-specific keys only.

### 15. `NoulQuestion` sketch contains a phantom field

**The flaw.** §2.1 shows `NoulQuestion` with a single field `instructions`, but
carries the comment: *"True = 'this is a Noul-style truth question'; kept explicit
so a backend that cannot express truth-judgments can reject cleanly."* There is no
such field — a leftover from a draft (probably a `kind` discriminator).

**Why it matters.** A spec with phantom fields erodes trust in the normative text:
is there supposed to be a kind tag that backends switch on? If yes, the translation
contract is incomplete; if no, the comment is noise. Small, but specs are read as
contracts.

**Fix.** Either add the field (e.g. `kind: Literal["noul"] = "noul"`) with its
contract, or delete the comment.

### 16. Over-engineering: what to cut while keeping the migration path

The doc builds: 3 Question dataclasses + 3 Answer dataclasses + `Question`/`Answer`
unions + `JudgmentResponse` + `JudgmentError` + `Judge` + `JudgmentSensor` +
`JudgmentGoalStrategy` + `JudgmentCallRecord`/`JudgmentStats` + `JevJudge` — 16 new
public symbols for Phase 1. Cuts that keep the migration path:
- **Merge `NoulAnswer` + `ScoreAnswer` → one `FloatAnswer(value, confidence)`.**
  They are structurally identical (`value: float`, `confidence: float | None`); the
  *question* type already disambiguates, and the correspondence check keys off the
  question anyway. One fewer parallel hierarchy to keep in sync.
- **Cut `retryable`** (attack #7). Nothing reads it.
- **Don't export the `Question`/`Answer` union aliases in `__all__`** unless runtime
  code needs them — they're annotation conveniences; 16 public symbols is a large
  frozen surface for an interface whose whole point is future revision.
- **Resolve the `descriptions` type honestly** (attack #6): if the first consumer
  (`JevGoalStrategy`) needs dict-valued criteria, `Mapping[str, str]` is the wrong
  core type. Either `Mapping[str, Any]` with a JSON-safe contract, or admit criteria
  rendering is adapter-specific and drop descriptions from core.
- Keep: `Judge`, `JudgmentError`, the three (or refined) Question types, answers,
  `JudgmentResponse`, both generic layers, records/stats, `JevJudge`. The protocol
  shape itself is the right size — the bloat is in parallel types and unconsumed fields.

---

## Constraint-violation check

| Constraint | Verdict | Notes |
|---|---|---|
| uv workflow | **PASS** | Test plan and commands assume `uv`; nothing proposes otherwise. |
| Python ≥ 3.12 | **PASS** | `X \| Y` unions, `dataclass(frozen=True)`, `Protocol` — all 3.12-clean. |
| 100% coverage, no `pragma: no cover` | **PASS (plan-level)** | §7 explicitly targets 100% with no pragmas; no uncovered-by-construction branches proposed. Watch: `goapauto.testing.fake_judge` falls under `source=["goapauto"]` — the 100% gate applies to it too (attack #13). |
| ruff + mypy green | **PASS (with note)** | Nothing in the sketches is inherently unlintable. Note: sketches show untyped `def judge(self, state, questions)` — the implementation must be fully annotated (mypy runs on `src/`; `disallow_untyped_defs` is off, but keep the codebase standard). `@runtime_checkable` on a methods-only `Judge` protocol is fine. |
| NO new required dependencies; core = stdlib + pydantic only | **PASS** | All proposed core types (`models/judgment.py`) use stdlib (`dataclasses`, `typing`, `collections.abc`) only — no `typesafe_sdk` import in core types, runtime-evaluated annotations, or docstring examples. §2.8's example imports `goapauto.models.jev` (the extra-gated module) only for `JevJudge` — allowed. Pydantic isn't actually used by the new types (frozen dataclasses instead) — not a violation, but a second modeling idiom in core; consider whether that's deliberate. |
| All new behavior opt-in / backward compatible | **PASS (Phase 1)** | Phase 1 adds a new module + `__init__` exports; zero changes to existing classes. Phase 2 delegation is correctly scoped out of Phase 1 — but see attacks #1, #5, #6: the delegation *plan* must be fixed before it's attempted. |
| Public API via `goapauto/__init__.py` `__all__` | **PASS** | §9 lists the additions; eager import is correct (no third-party imports in core). `JevJudge` stays lazy via the existing `_JEV_ATTRS` mechanism — consistent. |

No hard constraint violations. The failures are design-quality and cross-doc consistency, not constraint breaches.

## Interaction-consistency check

**01 — Runtime/replan budgets: AGREE, with one gap.**
Doc 01 §5(d) keeps the policy provider-free (`sensor_age: float | None`,
`plan_confidence: float | None`) — compatible with doc 05's untyped-confidence and
backend-agnostic posture. Gap: doc 05 §6(a) claims budgets are "enforced *around*
judgment" with `latency_ms` for attribution, but `latency_ms` is adapter-measured
and only exists post-call; doc 01 has no hook for an in-flight judgment. The
"timed block" treatment is asserted, not constructed (see attack #11).

**02 — Sensor caching/staleness: AGREE on placement, CONTRADICT on shape and granularity.**
- Agreement: both place caching strictly above the protocol; doc 02 §5(d) explicitly
  forbids cache fields on the protocol/response — doc 05 §5 honors this.
- Contradiction (protocol shape): doc 02 sketches the protocol as
  "`JudgeBackend.judge(observation) -> dict`" (backend returns *state values*;
  extraction below the protocol); doc 05 specifies
  `Judge.judge(state, questions) -> JudgmentResponse` (typed answers; extraction
  above, in the generic layer). These are incompatible designs — one must yield.
- Contradiction (granularity): doc 02 generalizes the cache to a **per-key**
  staleness ladder (`CachingSensor`, `goapauto/models/caching.py`); doc 05 ports
  `JevSensor`'s **whole-cache** semantics 1:1 into `JudgmentSensor` and calls it
  "the canonical one going forward". Both cannot be canonical. If doc 02's ladder
  lands, `JudgmentSensor`'s 1:1 port is already obsolete; if doc 05's whole-cache
  wins, doc 02's per-key generalization (which changes the dead-cache `{}` behavior
  to per-key omission) conflicts with the frozen semantics doc 05 pins.
- Telemetry: doc 02 adds `staleness: str = "fresh"` to `JevCallRecord`; doc 05's
  `JudgmentCallRecord` omits it (attack #5).

**03 — Action interruption: AGREE.**
Doc 03 §5(d): "No touchpoint. Interruption is orthogonal to how judgments are
produced." Doc 05 §6(c) says the same. Consistent — but jointly they bless a hung
judgment as permanently uninter ruptible (attack #11); the agreement is real, the
consequence is unowned.

**04 — Confidence gating: AGREE on the field, CONTRADICT on nearly everything else.**
- Agreement: confidence is `float | None` in the protocol; `None` = not reported.
- Contradiction (out-of-range): doc 04 raises `TypeSafeError`, fail-loud, test-pinned
  (attack #2); doc 05 mandates adapter clamping.
- Contradiction (Jev Noul confidence): doc 04 derives decisiveness `2·|noul−0.5|`,
  "Jev-backed callers never hit the `None` path"; doc 05 maps Noul/Score to `None`
  when absent (attack #3).
- Contradiction (error type): doc 04's gating raises `TypeSafeError`; doc 05's
  generic layers "never import SDK types" — the same validation cannot raise both.
- Divergence (absent policy): doc 04 pass-through + flagged vs doc 05's recommended
  treat-as-1.0 (attack #4).
- Structural: doc 04 builds gating as Jev-specific params (`_answer_confidence` in
  `jev.py`, "YAGNI" on generic gating); doc 05 defines the generic confidence
  contract but builds no generic gating. If both land, Jev has gating and generic
  backends don't — the generic confidence field has no generic consumer. Unowned work.

**06 — Why-diagnostics: AGREE on identity-in-trace, CONTRADICT on vocabulary and discovery.**
- Agreement: backend identity belongs in traces; doc 06's `judgment_gated` event
  fields (`backend`, `model`, …) match doc 05's `JudgmentResponse` fields.
- Contradiction (vocabulary): doc 06 §(b) says for `JevSensor`, backend is
  `"typesafe"`; doc 05 §2.5 fixes `JevJudge.backend = "jev"`. One backend, two names.
- Gap (discovery): doc 06 "prefers a `backend_name()` method when present" on the
  sensor; doc 05 provides a `backend` *attribute* on the response/adapter and **no
  sensor-level hook at all** — the inspector wraps sensors, not responses, so its
  preferred hook has no producer. Doc 05 §6(e) must specify how a sensor (not a
  response) exposes backend identity.

---

## Required changes before implementation (blocking)

1. Resolve attack #1: pick absorb (§3 + tests) or propagate (§4) for contract
   violations — delete or rewrite the losing section, and fix §2.8's NOTE.
2. Resolve attacks #2–#4 with doc 04's designer in the room: one confidence policy
   (recommend: validate-and-raise `JudgmentError` on out-of-range; SDK-reported
   confidence or `None`; pass-through + flagged for absent). Doc 04's test plan
   needs the matching edits.
3. Reconcile with doc 02: one protocol shape (`judge(state, questions) ->
   JudgmentResponse` vs `judge(observation) -> dict`) and one caching granularity
   (per-key ladder vs whole-cache port). Until then doc 05's "canonical" claim is
   contested.
4. Fix the backend vocabulary (`"jev"` vs `"typesafe"`) and add the sensor-level
   backend-identity hook doc 06 expects.
5. Rewrite the Phase 2 delegation section: mark it BLOCKED on open Q3 (legend
   derivation) and the criteria-rendering decision; replace "the unchanged test
   suite is the regression net" with the new characterization tests from attack #6;
   specify the telemetry error-name translation rule (attack #5).
6. Cut or replace `retryable` (attack #7); fix the `goapauto.testing` import trap
   (attack #13); unify construction errors as `ValueError` (attack #12); type `usage`
   (attack #14); delete the phantom `NoulQuestion` comment (attack #15).

## What survives (credit where due)

The non-goals are the strongest part of the doc: no second backend, no async, no
batching API, no over-abstraction — each with a stated reason rather than a shrug.
Caching above the protocol is the right placement and both caching-adjacent docs
agree on it. The `JevJudge`-as-adapter seam (rather than a `JevSensor` rewrite) is
the correct migration shape. The failure table's *taxonomy* is good even where its
handling column is wrong. None of the attacks above require rethinking the
interface's shape — only its contradictions.
