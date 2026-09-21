# RED-TEAM REVIEW — Design 04: Confidence Gating (opt-in, off by default)

**Reviewer:** red-team (hostile read)
**Target:** `~/workspace/goapauto-work/designs/04-confidence-gating.md`
**Date:** 2026-09-21
**Scope:** design doc only; no source modified, no state changed, `goapauto-proto/` and `goapauto-bench/` untouched.
**Codebase checked against:** `src/goapauto/models/jev.py` (0.5.0 `JevSensor._extract`, `JevGoalStrategy.select`, `JevCallRecord`/`JevStats`, `shared_client`), `src/goapauto/testing/fake_client.py`, `src/goapauto/models/goal.py` (`Goal.priority`), `src/goapauto/models/goal_arbitrator.py` (`PriorityGoalStrategy`), `src/goapauto/__init__.py` (`__all__`). Designs 01, 02, 03, 05, 06 read in full (interaction sections cross-checked line by line).

---

## Verdict: NEEDS-REWORK

The doc is well-written and its opt-in discipline is genuine, but it has faults that cannot be fixed at implementation time: (1) its cache-write rule (`self._cached = updates`, the gated view) **permanently evicts last-good judgments** on a single low-confidence blip and is unimplementable against Design 02's per-key cache, which 04 claims to compose with; (2) it **directly contradicts Design 02** on the staleness×confidence composition (multiplicative decay vs. conjunction — both docs claim the gating design owns the decision); (3) it **ignores Design 06's binding producer contract** (`record_judgment_gated`, including proceed-events) while unilaterally declaring "no separate event bus"; (4) the Noul "decisiveness proxy" is a category error — decisiveness is not confidence — and the doc's own open Q1 concedes the central case against it. The cache semantics, the 02 composition, the 06 contract, and the Noul question need redesign, not polish.

## The three strongest attacks

1. **A single low-confidence answer permanently evicts the last good cached judgment** — 04 §3.3 replaces the whole cache with the gated view, so one gated call followed by one `TypeSafeError` leaves the stale path replaying `{}` even though a high-confidence judgment existed one call earlier; this also contradicts 02's per-key "absent keys keep their old value" rule.
2. **02 and 04 decide the staleness×confidence composition in opposite ways** — 02 §5c: `effective_confidence = confidence * staleness_discount(meta)` applied by the gating worker; 04 §5b: "staleness does not decay confidence… the two compose by conjunction." Both claim the gating design owns it. Both cannot ship. (The sibling 02 review independently reached NEEDS-REWORK on this same contradiction.)
3. **The Noul decisiveness proxy (`2·|noul−0.5|`) is not confidence and should be cut** — a calibrated 0.6 is a confident probabilistic judgment, not an unsure one; with any threshold > 0 every honest 0.5 ("I don't know") is silently dropped; and the threshold becomes meaningless for Noul (it only sets the width of the drop-band around 0.5).

---

## Numbered attacks

### 1. Whole-cache replacement on a gated write destroys the fallback cache

**Flaw.** 04 §3.3: on a successful call, `self._cached = updates` — the *gated view* (passing questions only). A question that gates is not merely withheld from this cycle's dict; its previously cached value is deleted from the cache. The test plan makes this deliberate: "all-questions-gate → `{}` with `last_success` advanced (a subsequent forced error replays `{}` from cache, not older values)."

**Why it matters — concrete scenario.** `threat` judged at confidence 0.95, cached. Next sense: observation changed, model says `threat=0.85` at confidence 0.4 < threshold → gated, cache becomes `{}` (threat evicted). Next sense: `TypeSafeError` (network blip) → stale path replays the cache, which is `{}` → downstream gets **no** threat update at all, even though a 30-seconds-fresh, 0.95-confidence judgment existed two calls ago. The design's stated goal is "a judgment the model is unsure about should not become a world-state fact" — but the *old* judgment was sure, and the design punishes it for the *new* judgment's uncertainty. One low-confidence blip followed by one network blip = total perception loss on that key, strictly worse than 0.5.0 behavior.

**Fix.** Per-key merge, not wholesale replacement: gated questions keep their previous `(value, as_of)` cache entries; only passing questions refresh. This is exactly 02 §3.4's rule ("keys that disappear from the wrapped output keep their old value under the ladder until DEAD, then vanish") and 02 §3.6 edge 5 ("empty `sense()` dict from inner sensor: refreshes nothing; existing cached keys keep aging"). 04's current rule contradicts both. Note the design's defense — "a stale cached judgment that 'would also gate' today cannot exist" — is irrelevant to the bug: the evicted judgment *passed* the gate when cached; the problem is evicting it at all.

### 2. Staleness×confidence composition: 02 and 04 decide opposite things

**Flaw.** 02 §5c: "They compound multiplicatively… `effective_confidence = confidence * staleness_discount(meta)`… The gating worker multiplies its per-value confidence by this discount." 04 §5b: "Staleness does **not** decay confidence. Confidence is a property of the judgment at answer time… The two compose by conjunction: a judgment reaches the world state only if it was confident-enough when made AND fresh-enough when replayed." Both sections claim the *gating design* owns the composition decision ("owned by the gating design" in 02; "the gating worker" is 04's).

**Why it matters.** This is not a wording difference, it is a different gate function. Take `threat` judged at confidence 0.9, replayed from cache at age 15s with `stale_after=5, max_stale=30`: under 02's rule effective confidence is `0.9 × (1 − 10/25) ≈ 0.54` → gates at `t=0.6`; under 04's rule the cached judgment already passed at write time, is fresh-enough at read time, and is replayed ungated. An implementer cannot satisfy both docs. The sibling 02 review flagged this independently from the other side.

**Fix or question.** Pick one, in one doc, and delete the other. My read: 04's conjunction is the cleaner composition (confidence is a property of the judgment event; age is a property of the replay event; multiplying them invents a quantity neither backend reported). But whichever wins, 02's `staleness_discount` helper and 04's §5b must be edited to match — today they are mutually exclusive.

### 3. The Noul decisiveness proxy is a category error — take the hard side: cut it

**Flaw.** 04 §3.1 defines Noul "confidence" as `2.0 * abs(noul - 0.5)` and feeds it through the same threshold comparison and the same `confidence=` log/telemetry fields as model-reported confidence. The doc's own open Q1 admits the central objection and punts it.

**Why it matters — four concrete problems:**
- (a) A Noul answer *is* a probability estimate. A calibrated model reporting 0.6 is fully confident *that the probability is 0.6*; the 0.6 is the content, not the uncertainty. The proxy relabels confident probabilistic judgments as "unsure" — a category error, not an approximation.
- (b) With **any** threshold `t > 0`, a Noul of exactly 0.5 yields confidence 0.0 and **always gates** (§3.5 admits this). Enabling gating at `t=0.1` silently converts every honest "I don't know" into a dropped update — in domains where the planner was built to handle mid-range probabilities (the reason Noul exists instead of bool), the design punishes the model's honesty.
- (c) The threshold becomes meaningless for Noul: it no longer means "minimum acceptable confidence" but "half-width of the drop-band around 0.5." Users tuning `t` will be tuning a quantity the doc never names.
- (d) The docstring caveat ("deliberately not presented as model-reported confidence") does not survive the doc's own observability: the §2.3 log line prints `confidence=0.42` for a Noul, and `JevCallRecord` carries no `confidence_is_proxy` marker. Downstream (and 06's trace) will read it as reported confidence.

**Fix.** Make Noul ungatable: Noul answers always pass (their uncertainty is already expressed in the value the planner consumes). If a future backend reports real Noul confidence, the provider-interface design (05) is the place to specify it — 05's core `NoulAnswer` already has `confidence: float | None`, which is the honest shape. Do not ship the proxy.

### 4. Design 06's binding producer contract is unimplemented and contradicted

**Flaw.** 06 §6d defines the `judgment_gated` trace event with **binding** field names (`{backend, model, question, value, confidence, threshold, decision, context, latency_ms, error}`, `decision ∈ {"proceed","blocked","flagged","escalated"}`) and states: "Producer: the gating worker calls `inspector.record_judgment_gated(...)`… Every gated judgment is a trace event — including `decision: "proceed"`. A gate that only logs blocks is a lie by omission." 04 never mentions `record_judgment_gated`, emits no `judgment_gated` events, and §5e unilaterally declares: "No separate event bus is introduced by this design — the telemetry record *is* the event; the diagnostics design should ingest its new fields." 06 already defined the event; 04 doesn't produce it.

**Why it matters.** Under 06's contract, 04's implementation would be non-compliant on three counts: (i) no producer call exists; (ii) 04's observability has **no proceed visibility at all** — §2.3 logs only gated drops and absent-confidence pass-throughs, so 06's core debugging question ("why did it act on 0.51 confidence?") is unanswerable from 04's channels; (iii) 06's open Q7 explicitly asks "the gating worker should confirm this contract" (fail-loud `ValueError` on a detached inspector) — 04 never answers. Also unmapped: 04's outcomes ("update dropped", "low_confidence fallback") to 06's vocabulary ("blocked"? "flagged"?), and 06's `"escalated"` (a 04 non-goal) hanging in a vocabulary 04 never addresses.

**Fix.** 04 must either adopt the `record_judgment_gated` producer call (including proceed-events, which means recording per-question outcomes even when the gate passes — a real cost/verbosity decision the doc currently ducks) or negotiate the contract change with 06. "The telemetry record is the event" is not a negotiation; it's a refusal.

### 5. NaN handling contradicts 05's already-decided adapter normalization

**Flaw.** 04 §3.1: NaN or out-of-range confidence → raise `TypeSafeError` ("clamping was considered and rejected"). 05 §2.2: "Adapters MUST clamp out-of-range values (or map them); the protocol guarantees the range, **so gating layers never see 1.7 or -0.2**." 05's `JevJudge` adapter (step 4) clamps; 04's `JevSensor` raises. For the same corrupt confidence value, the two designs prescribe opposite behavior — and 05 §2.6 plans Phase-2 delegation of `JevSensor` onto `JevJudge`+`JudgmentSensor`, at which point the raise-vs-clamp fork becomes a behavioral fork inside one call path.

**Why it matters.** 04's open Q2 ("Clamp vs. raise… Which failure posture does Malcom want?") re-asks a question 05 already answered for the adapter layer. If Malcom answers "raise," the Phase-2 delegation needs a retranslation rule 05 doesn't specify; if "clamp," 04's §3.1/§4-table/failure-mode rows must be rewritten. Either way the docs must agree *before* implementation.

**Question for the designer.** Is 04's fail-loud-on-NaN scoped to "the Jev path in Phase 1, pre-delegation," with 05's clamp rule governing thereafter? If so, say so explicitly and reconcile Q2 with 05 §2.2 instead of presenting it as undecided.

### 6. The doc's own pseudocode violates its "byte-identical / zero overhead" guarantee

**Flaw.** 04 §4 promises: threshold `None` = "feature off, zero overhead and zero telemetry fields populated." The test plan promises "gating off means byte-identical behavior." But the §3.3 sketch computes `conf = _answer_confidence(question, answer)` **before** the `if threshold is not None` check — i.e., on every question of every `sense()` call, even with gating fully off. Same in the §3.4 strategy sketch. `_answer_confidence` reads `answer.confidence` (attribute access the 0.5.0 path never performs) and, per §3.1, *validates* the range — so a backend emitting NaN confidence would raise `TypeSafeError` with gating **off**, where 0.5.0 passes silently. That is a behavior change under default arguments, contradicting the "ALL new behavior opt-in" constraint and the "byte-identical" test-plan claim. (The existing suite wouldn't catch it: `FakeTypeSafeClient` always sets `confidence=1.0`.)

**Fix.** Hoist the threshold check above any confidence access: if the effective threshold is `None`, skip `_answer_confidence` entirely — no attribute read, no validation, no float ops. Then "zero overhead" is true and the fail-loud validation is genuinely scoped to the gating path, which also resolves the ambiguity in §3.2 ("No check" must mean *no check at all*, including validation).

### 7. Fully-gated `{}` is indistinguishable from dead-cache `{}` — and 01 will misread it

**Flaw.** 04 §3.3: all questions gate on a fresh call → return `{}`, "not an error." At the `sense() -> dict` contract, this `{}` is byte-identical to the max_stale dead-cache `{}`. The only distinguishing channel is the telemetry callback — a side channel `SensorManager.update_state` never sees. 02's `SensorReport`/`sense_detailed()` doesn't help either: a gated key vanishes from the cache, so it's absent from both `values` and `meta` — the detailed channel also can't distinguish "gated" from "never answered."

**Why it matters — concrete scenario with 01.** 01 §5a instructs the host: "when `JevSensor` degrades to `{}` (older than `max_stale`), the host should pass a large `sensor_age` so the policy stops replanning on diffs computed from garbage" (`SENSORS_STALE` suppresses world-change replans). A fully-gated *fresh* call returns the same `{}`; a host following 01's guidance suppresses replans — but perception is fresh, the model answered, it just wasn't confident. Meanwhile 01's own `min_plan_confidence → LOW_CONFIDENCE` replan *bypasses* the interval in the opposite direction. So low confidence simultaneously means "suppress replans, sensors are stale" (via the `{}` conflation) and "force a replan, confidence is low" (via 01's confidence path), depending on which signal the host reads. The doc waves this away with "not an error"; it is a signal-conflation bug at the exact boundary 01 consumes.

**Fix or question.** Either give the gated-empty case a distinguishable shape (02's report needs a gate marker per key, or `sense()` needs a companion signal — both are 02/04 joint work), or document precisely how a host distinguishes "fresh but unconfident" from "dead cache" when feeding 01's `sensor_age`. "Check the telemetry callback" is not an answer: telemetry is per-call fire-and-forget, not queryable state.

### 8. Gate-kept world-state values are stale data with their age discarded

**Flaw.** Non-goal 5 says a gated update means "the key keeps its previous value" in the world state. But §3.3 advances `last_success`/`last_call` on the gated call and replaces the cache with the gated view. So: the world state keeps value *v* (written at T₀), the cache no longer contains the key at all, and the sensor's clock says "perception is fresh as of T₁." The kept value's age is now **untracked anywhere** — 02's per-key `as_of` bookkeeping loses the key entirely (it's not in the cache), and the world state carries no metadata by design.

**Why it matters.** This is the task's "stale data wearing a fresh face" scenario, slightly worse: it's stale data wearing *no* face. A `threat=0.9` kept through three gated cycles is presented to the planner with the same standing as a fresh 0.9, while 02's `SensorReport` for that key shows nothing (key absent from cache) — the staleness ladder, the `max_age` helpers, and the interruption worker's FRESH→STALE transition detection all go blind on exactly the keys gating touched. 04's reassurance — "the staleness clock still advances (the model answered; perception is fresh)" — is false for the gated keys: the model answered, but the *kept value* is old, and its age was just discarded.

**Fix.** Follows from Attack 1's fix: keep the previous `(value, as_of)` cache entry for gated keys (don't advance their age), so 02's ladder continues to age them honestly toward STALE/DEAD. "The model answered" must not rewind the clock on data the model didn't confidently provide.

### 9. Strategy: gate-before-label-match destroys the unknown-label signal, and the pick itself is never recorded

**Flaw.** §3.4 runs the gate *before* label matching, and §3.5 collapses "low confidence + unknown label" into `fallback_reason="low_confidence"`. An unknown label is a *contract* signal (the model isn't using the provided label set — prompt drift, schema change); low confidence is an *uncertainty* signal. When both hold, the doc reports only the latter. Worse: `JevCallRecord` carries no pick label at all — when the strategy gates, the single most diagnostic datum (what the model actually chose) is dropped from every one of 04's observability channels. (06's `JevDecisionInfo.choice` has the field; 04's record doesn't.)

**Why it matters — concrete scenario.** Soulscape's goal prompt drifts; the model starts returning `"defend_base"` when the label is `"defend"`. Every arbitration now falls back. The operator sees `fallback_reason="low_confidence"` and tunes thresholds — the actual fix (repair the label set) is invisible because the unknown label was swallowed by the gate ordering and the pick was never logged.

**Fix.** Record both facts: keep the gate-first ordering if you must, but put the pick label and a `label_matched: bool` into the telemetry record (and the log line). A single-string `fallback_reason` cannot carry a conjunction; either make it a tuple/set of reasons or add the matched flag alongside.

### 10. `threshold=0.0` vs `None`: the "distinguishable in diagnostics" claim is false

**Flaw.** §3.2: "`t = 0.0` therefore gates nothing (enabled but vacuous — distinguishable from `None` in diagnostics)." With `t=0.0`: no question ever satisfies `c < 0.0` (given validated `c ≥ 0`), so `gated_questions=()`, `gated=False`, zero log lines — **byte-identical telemetry to `None`**. There is no field, anywhere in §2.3, that records "gating was enabled." The distinguishability claim is false on the doc's own schema, and the test plan's "`threshold=0.0` gates nothing; `threshold=None` leaves telemetry fields at defaults" describes two indistinguishable outcomes.

**Fix.** Either add the distinguishing field (e.g. record the effective threshold per question in the record, or a `gating_enabled: bool`) or delete the claim and admit 0.0-vs-None is observable only in constructor intent. Also note the interaction with Attack 6: if validation is tied to "enabled," then `t=0.0` raises on NaN while gating nothing — the vacuous-but-validating corner the doc never addresses.

### 11. The "inclusive boundary" promise is near-meaningless for derived confidences (float epsilon)

**Flaw.** §3.2: "gate iff `c < t` (strictly less). The boundary is inclusive: `c == t` passes." For the Noul proxy this is arithmetic fiction: `2.0 * abs(0.9 - 0.5)` in binary float is `0.8000000000000000444`, not `0.8` — a model reporting exactly 0.9 with `t=0.8` passes, but only by epsilon luck; a symmetric computation elsewhere could land just below. The test plan's "confidence exactly equal to threshold passes" test will pass with hand-constructed floats and prove nothing about real backends, where reported confidences arrive through JSON serialization anyway.

**Why it matters.** Threshold tuning against a strict `<` with float-derived confidences gives users an illusory precision: the effective boundary is `t ± 1ulp` of whatever arithmetic produced `c`. This is minor for model-reported confidences (backend-quantized) but structural for the derived proxy — one more reason Attack 3's cut is the clean fix. At minimum the doc should state the boundary is exact-decimal only for exact inputs and not promise inclusivity as a semantic guarantee.

### 12. Absent-confidence semantics: 05 says "treat as 1.0," 04 says "pass through flagged as unknown"

**Flaw.** 05 §6d: "the recommended default is *treat-absent-as-1.0* (i.e., a backend that cannot report confidence is trusted at face value…). The alternative (treat-absent-as-0.0) would make every non-probabilistic backend fail every gate." 05 §9 summarizes: "gating treats absent as 1.0." 04 §3.2: absent confidence → "pass-through, recorded in `unknown_confidence_questions` / `confidence_absent`… failing open on the unknowable, loudly."

**Why it matters.** Both pass the value through, but they *reason* about it differently and record it differently: 05's framing says absent ≡ confident (no flag needed, no doubt invented); 04's framing says absent ≡ unknowable (flagged, logged per occurrence, separate telemetry fields). When 04's gating is later applied to 05's `JudgmentSensor` (whose `FakeJudge` reports `confidence=None` by default), which semantics governs the trace — "trusted at 1.0" or "flagged unknown"? The per-occurrence `logger.info` for absent-confidence pass-throughs also means a `FakeJudge`-backed sensor spams info logs on every call — the "no telemetry noise" promise (§3.2, for the `None`-threshold case) has no analogue for the absent-confidence case.

**Question for the designer.** Is absent confidence 1.0 (05) or unknown (04)? If unknown-but-passing, the log-per-occurrence needs a rate or level rethink; if 1.0, the `unknown_confidence_questions`/`confidence_absent` fields are redundant with "proceed."

### 13. 01's `plan_confidence` / `min_plan_confidence` has no producer in 04's scope

**Flaw.** 01 §5c, titled "(c) Confidence gating," describes the host "(or (c)'s wrapper)" scoring the fresh `PlanResult` and passing `plan_confidence=` into `should_replan`, with `min_plan_confidence` converting low confidence into a `LOW_CONFIDENCE` replan. 04's scope is sensor judgments and goal picks only — plan-confidence scoring is never mentioned, and 04's telemetry (`JevCallRecord`, `JevStats`) has no plan-level fields to feed 01's `plan_confidence: float | None`.

**Why it matters.** Two docs use the name "confidence gating" for two different layers (judgment gating vs. plan-confidence scoring), and 01's `LOW_CONFIDENCE` replan trigger currently has no producer anywhere in the six designs. A reader implementing 01 will look to 04 for the `plan_confidence` source and find nothing.

**Fix.** Either widen 04's scope statement to disclaim plan-confidence explicitly (pointing at the future owner), or correct 01's §5c to stop attributing plan scoring to "confidence gating (design 04)." The name collision will otherwise generate exactly the wrong implementation.

### 14. Per-question thresholds for questions absent from `mapping` are dead config

**Flaw.** `confidence_thresholds` is validated against `questions` keys (§2.1), but `_extract` iterates `self._mapping.items()` — and a custom `mapping` may omit a question (it's asked via `system_one(observation, self._questions)` but its answer is never extracted). A threshold entry for such a question validates cleanly and is never consulted. Silent dead config.

**Fix.** Validate `confidence_thresholds` keys against the *effective* mapping keys (the questions actually extracted), or warn when a threshold names a question with no mapped state key. Fail-loud applies to gating too (§1) — dead thresholds are silent.

### 15. Over-engineering: what to cut

- **Cut the per-question `None` override** (`{"cover": None}` to exempt one question from a global). It adds a third state to every threshold lookup for a convenience case already expressible (set per-question floats, no global). The doc's own open Q5 wobbles on it — resolve the wobble by cutting.
- **Cut `fallback_policy`, or at minimum `"priority"`.** The doc admits the inherited `"first"` fallback "was never a designed choice" (open Q3) — so the design adds a knob choosing between a non-choice and a guess, with zero Soulscape data, doubling the strategy's fallback test matrix. Ship gating with the existing fallback; decide first-vs-priority once, with data, as its own change. (Note: `"priority"` as specified is behavior-identical to `PriorityGoalStrategy.select` — verified against `goal_arbitrator.py` — so the honest version of this knob is a two-line delegation, not a `Literal` policy framework.)
- **Keep:** global + per-question float thresholds (different questions have genuinely different costs of error — `threat` vs `room_name`), `JevStats.gated` (calibration needs the counter), the fail-loud validation *within* the gating path.
- **Don't build yet:** anything resembling a generic `ConfidenceGate` — the doc correctly YAGNIs this (§5d), hold the line there.

### 16. Minor: two namespaces for one key; duplicated priority logic

- `confidence_thresholds` is keyed by **question name**, not state key — consistent with `mapping`, but users think in world-state keys, and the doc needs the bolded warning it already has. Consider accepting state keys and resolving through the mapping; at minimum the `TypeSafeError` message should suggest the question-name namespace.
- `"priority"` inlines `min(goals, key=lambda g: g.priority)` instead of delegating to `PriorityGoalStrategy`. Importing it would **not** cycle (`goal_arbitrator` doesn't import `jev`) — the task's suggested worry is unfounded — but the duplication is drift risk for zero benefit. Delegate or cut (see 15).

---

## Constraint-violation check

| Constraint | Verdict | Notes |
|---|---|---|
| uv workflow | **Pass** | Design-only; test plan implies `uv run pytest`. No new tooling. |
| Python ≥ 3.12 | **Pass** | `float \| None`, `Literal[...]`, defaulted `tuple` fields in a frozen dataclass — all fine on 3.12. |
| 100% coverage, no `pragma: no cover` | **Pass with caveats** | Test plan enumerates every new branch. Caveats: (i) the absent-confidence path's "full-backend coverage" is deferred to 05's tests — a cross-doc coverage obligation that should be named as such; (ii) the `t=0.0`-vs-`None` distinguishability claim (Attack 10) is untestable as specified — no observable difference exists to cover. |
| ruff + mypy green | **Pass (design-level)** | Nothing in the API shape threatens either; the sketch's long ternary is pseudocode. |
| No new required dependencies | **Pass** | Only `typesafe_sdk` (existing `[jev]` extra). |
| All new behavior opt-in; default behavior of every existing API unchanged | **FAIL as written** | Two violations: (a) the §3.3/§3.4 sketches compute `_answer_confidence` (attribute access + range validation) *before* the threshold check, so gating-off is not "zero overhead" and NaN confidence would raise `TypeSafeError` where 0.5.0 passes silently (Attack 6); (b) the strategy's unified `_fallback` is claimed log-identical, but that's a claim about the implementation, and the sketch's reorder (gate before label match) changes which reason is reported for the unknown-label case (Attack 9). Fix (a) by hoisting the threshold check; then this row passes. |
| Public API via `goapauto/__init__.py` `__all__` | **Pass** | No new top-level exports; `JevCallRecord`/`JevStats` field additions are additive to already-exported symbols. |

Additional backward-compat verification performed: `JevCallRecord` is a `@dataclass(frozen=True)` (confirmed in source) — appending defaulted fields preserves existing positional constructions, so the doc's claim holds. (No test constructs it positionally today — grep over `tests/` finds zero constructions — so the claim is true but unpinned; fine.) `shared_client`, `JevSensor.judge`, and `FakeTypeSafeClient` signatures/semantics are untouched by the design — holds. `FakeTypeSafeClient` builds `ChoiceAnswer(confidence=1.0)` / `ScoreAnswer(confidence=1.0)` and bare `NoulAnswer(noul=…)` — matches the doc's §3.5 claims.

---

## Interaction-consistency check (doc by doc)

### 01 — Runtime / replan budgets: MIXED (one agreement, two gaps)
- **Agreement.** 04 §5a (no extra model calls; a gated judgment counts as a completed call for budget accounting) is consistent with 01's treatment of `sense()` as a timed block. No contradiction on budgets.
- **Gap (hard).** 01 §5a's `sensor_age` guidance + 04's fully-gated `{}` conflation (Attack 7): 01 tells the host to treat `{}` as dead cache; 04 produces `{}` from fresh perception. The host cannot follow both docs correctly.
- **Gap (scope).** 01 §5c attributes plan-confidence scoring (`plan_confidence=` → `should_replan`) to "confidence gating (design 04)"; 04 builds no plan scoring (Attack 13). Name collision, no producer.

### 02 — Sensor caching / staleness: CONTRADICTION (fail)
- **Agreement (slogan only).** Both docs say "gate at write, replay at read" — but see below; the agreement is verbal, not mechanical.
- **Contradiction 1 (hard).** Composition rule: 02 §5c multiplicative `staleness_discount` applied by the gating worker vs. 04 §5b conjunction, no decay (Attack 2). Both claim the gating design owns the decision.
- **Contradiction 2 (hard).** Cache-write semantics: 04 §3.3 whole-cache replacement with the gated view vs. 02 §3.4/§3.6-edge-5 per-key "absent keys keep their old value; empty output refreshes nothing" (Attack 1). 04 was designed against the 0.5.0 whole-cache it claims to compose with 02's per-key replacement of — its §3.3 is unimplementable once 02 lands.
- **Gap.** 02's `SensorReport`/`ValueMeta`/`sense_detailed()` carry no gate fields; a gated key is absent from both `values` and `meta` (Attack 7). 02's `JevCallRecord.staleness` additive field composes cleanly with 04's additive fields — the one mechanical agreement.
- **Consequence.** 04's "staleness clock still advances" (Attack 8) discards the age of gate-kept values that 02's ladder is supposed to track.

### 03 — Action interruption: AGREEMENT
- 04 §5c ("gated mid-plan judgment does not interrupt; perception-boundary only, no signal") matches 03 §5c ("A gated (low-confidence) judgment mid-plan does not interrupt the in-flight plan… Gating is a planning-time concern"). 03's sanctioned reverse touchpoint (confidence layer *may* call `interrupt(source="confidence")`) is host-initiated and doesn't contradict 04 emitting nothing. Clean.

### 05 — Provider-independent judgment interface: MIXED (one agreement, two contradictions)
- **Agreement.** Optional-confidence contract: 04 §3.2 (absent → pass-through, never coerced to 0.0) matches 05 §2.2/§6d (`None` = not reported, never 0.0). Genuine alignment.
- **Contradiction 1 (hard).** Corrupt confidence: 04 raises `TypeSafeError` (clamping "considered and rejected") vs. 05 "adapters MUST clamp… gating layers never see 1.7 or -0.2" (Attack 5). 04's open Q2 re-asks what 05 decided.
- **Contradiction 2 (semantic).** Absent-confidence *meaning*: 05 "treat-absent-as-1.0 / trusted at face value" vs. 04 "failing open on the unknowable, loudly" with flagged fields and per-occurrence logs (Attack 12).

### 06 — Why-diagnostics: CONTRADICTION (fail)
- **Agreement (intent only).** Both want every gated decision observable. That's where agreement ends.
- **Contradiction (hard).** 06 §6d bindingly requires the gating worker to call `inspector.record_judgment_gated(...)` for **every** gated judgment **including proceed**, with a fixed vocabulary (`proceed|blocked|flagged|escalated`); 04 §5e declares "no separate event bus… the telemetry record *is* the event" and specifies no proceed visibility at all (Attack 4). 06's open Q7 asks the gating worker to confirm the detached-inspector fail-loud contract — 04 never answers.
- **Gap.** 04's telemetry lacks the judged value / pick label that 06's event schema carries (`value`, and `JevDecisionInfo.choice`), so even the fields 04 *does* emit don't cover 06's "why" (Attack 9).

---

## What must change before implementation (in priority order)

1. Redefine the cache write as a per-key merge (Attack 1) — this also fixes Attack 8 and the 02-Contradiction-2.
2. Resolve the 02/04 composition contradiction in exactly one place (Attack 2) — my vote: 04's conjunction, delete 02's `staleness_discount`.
3. Cut the Noul decisiveness proxy (Attack 3) — Noul ungatable, like the doc's own alternative.
4. Adopt or renegotiate 06's `record_judgment_gated` producer contract, including proceed-events and the outcome vocabulary mapping (Attack 4).
5. Reconcile NaN/out-of-range handling with 05's adapter clamp rule (Attack 5).
6. Hoist the threshold check above confidence access in `_extract`/`select` (Attack 6) — this repairs the opt-in constraint row.
7. Cut `fallback_policy` and the per-question `None` override (Attack 15); record the pick label + label-matched flag in telemetry (Attack 9).
