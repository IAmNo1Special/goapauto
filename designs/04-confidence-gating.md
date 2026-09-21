# 04 — Confidence Gating (opt-in, off by default)

**Status:** implemented (Phase 2, 0.6.0). `GateDecision`, per-question thresholds, strategy gating (opt-in, off by default).
**Scope:** `JevSensor` (sensor mapping) and `JevGoalStrategy` (goal arbitration).
**Default:** fully off. Every existing API keeps its exact current behavior unless
the caller explicitly enables gating.
**Revision:** R2 (2026-09-21) — incorporates the red-team review (16 attacks)
and the coordinator's binding rulings (A, A1, C, D + threshold semantics).

---

## 1. Goal and non-goals

### Goal

Give callers an opt-in way to stop low-confidence TypeSafe judgments from
reaching the planner: a judgment the model itself is unsure about should not
become a world-state fact, and a goal pick the model is unsure about should
not override the developer's static priority order. Every gated decision must
be observable — fail-loud applies to gating too, so "dropped silently" is not
an allowed outcome.

### Non-goals (explicitly NOT built, and why)

1. **Automatic / adaptive threshold tuning.** Thresholds are set by the caller
   and never move at runtime. A feedback loop that moves thresholds would make
   agent behavior non-reproducible and would violate the explicit-fallback
   requirement. Calibration is the user's job, aided by the telemetry below.
2. **Escalation to a bigger / more expensive backend on low confidence.** That
   is unbounded cost and directly conflicts with the runtime/replan budget
   design (05's sibling; see §5a). A caller that wants escalation can wire it
   manually from the telemetry callback — the record tells them exactly which
   question gated.
3. **Re-querying the model when confidence is low.** Same reason as (2), plus
   it would make `sense()` latency unpredictable. One call, one answer, gate
   the answer.
4. **"Keep current goal" as a strategy fallback.** `select()` receives no
   notion of the current goal and `GoalArbitrator` keeps no selection memory;
   that would be arbitrator state, which is another design's turf. Fallback is
   to a static rule (first / priority), never to hidden state.
5. **Deleting keys or marking keys "unknown" in the world state on gate.**
   Gating drops the *update*; the key keeps its previous value (or stays
   absent). Deletion is destructive, cannot be expressed through the
   updates-dict contract (`sense() -> dict[str, Any]`), and would surprise
   every downstream consumer.
6. **Confidence smoothing / temporal fusion across calls.** Averaging
   confidences over time is the caching/staleness design's domain. Gating is
   per-call, at the perception boundary.
7. **Plan-confidence scoring.** This design gates *judgments* (sensor answers,
   goal picks). The `plan_confidence` input to 01's `should_replan` is a
   different layer — scoring a finished plan from its component confidences —
   and has no producer in this design. The name collision ("confidence
   gating" in 01 §5c vs. this doc) is documented in §5a; the scorer belongs to
   a future design.

---

## 2. User-side API

### 2.1 Sensor-level gating — `JevSensor`

```python
from typesafe_sdk import Noul, Choice
from goapauto.models.jev import JevSensor

sensor = JevSensor(
    observe=observe,
    questions={"threat": Noul("..."), "cover": Choice(...)},
    # NEW — all appended after existing params, all default-off:
    confidence_threshold=0.6,            # float | None = None; None = gating off
    confidence_thresholds={              # dict[str, float] | None = None
        "threat": 0.8,                   # per-question override (keyed by QUESTION name)
    },
)
```

- `confidence_threshold`: global default applied to every extracted question.
- `confidence_thresholds`: per-question overrides, keyed by **question name**
  (the key in the `questions` dict), not by mapped state key — gating happens
  at answer extraction, before mapping.
- **Precedence (exact):** a per-question entry wins when present; otherwise the
  global applies; if neither is set for a question, gating is off for it.
- Per-question values must be floats in `[0.0, 1.0]` — a `None` value in the
  dict is rejected at construction (`ValueError`); there is no per-question
  opt-out entry (the red-team's cut — if you want per-question-only gating,
  set floats and leave the global `None`).
- Constructor validation (fail-fast): any threshold not in `[0.0, 1.0]`
  raises `ValueError`; a `confidence_thresholds` key naming a question that is
  **not actually extracted** (absent from the effective `mapping`) raises
  `TypeSafeError` (fail-loud against dead config — a threshold that can never
  be consulted is a bug, not a preference).

Behavior when enabled: in `_extract`, a question whose effective confidence
is below its threshold has its update **dropped** — the key is absent from
the returned updates dict, so the world state keeps its previous value. The
drop is recorded (telemetry + log + stats, §2.3).

### 2.2 Strategy-level gating — `JevGoalStrategy`

```python
from goapauto.models.jev import JevGoalStrategy

strategy = JevGoalStrategy(
    client=client,
    confidence_threshold=0.7,   # float | None = None; None = gating off
    fallback_policy="priority",  # Literal["first", "priority"] = "first"
)
```

- One implicit question (the goal `Choice`), so a single threshold suffices —
  no per-question map.
- `fallback_policy` unifies all three fallback triggers through one helper
  (see §3.4): `"first"` reproduces today's behavior exactly (return
  `goals[0]`); `"priority"` returns the highest-priority active goal by the
  same rule as `PriorityGoalStrategy.select` (see §3.4 for the delegation).
- `confidence_threshold` validated to `[0.0, 1.0]` at construction
  (`ValueError` otherwise).

### 2.3 Observability (the fail-loud requirement)

New frozen dataclass (the binding trace shape; see §9):

```python
@dataclass(frozen=True)
class GateDecision:
    question: str            # question name ("goal" for the strategy pick)
    key: str | None          # mapped state key; None for the strategy pick
    confidence: float | None # effective confidence; None = backend reported none
    threshold: float          # effective threshold applied
    decision: str            # "proceed" | "blocked"
    reason: str              # "passed" | "low_confidence" | "absent_confidence"
    fallback: str | None     # action taken on block: "drop" (sensor),
                             # "first" | "priority" (strategy); None on proceed
```

`JevCallRecord` gains defaulted fields (backward compatible — existing
positional constructions keep working). Appended in this order (see §9 for
the 02 merge note):

```python
@dataclass(frozen=True)
class JevCallRecord:
    source: str
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    error: str | None
    stale_cache_hit: bool
    # NEW (sensor + strategy):
    gate_decisions: tuple[GateDecision, ...] = ()  # per-question outcomes, proceed AND blocked
    gated_questions: tuple[str, ...] = ()          # sensor: question names BLOCKED this call
    unknown_confidence_questions: tuple[str, ...] = ()  # sensor: passed through, confidence absent
    gating_enabled: bool = False                   # any effective threshold non-None (distinguishes 0.0 from None)
    # NEW (sensor):
    fully_gated: bool = False                      # gating on and every extracted question blocked
    # NEW (strategy):
    gated: bool = False                            # the pick was blocked
    confidence_absent: bool = False                # pick passed through, confidence absent
    pick_label: str | None = None                  # what the model actually chose (recorded even when blocked)
    label_matched: bool = True                     # whether pick_label matched a goal label
    fallback_reason: str | None = None             # "error" | "unknown_label" | "low_confidence"
```

Notes:

- `gate_decisions` carries **proceed** decisions too: a gate that only logs
  blocks cannot answer "why did it act on 0.51 confidence?" Decisions are
  recorded only for questions where gating was consulted (threshold non-`None`
  for that question); when gating is fully off the tuple is empty and no
  confidence attribute is ever read (§3.2).
- `gating_enabled` distinguishes `threshold=0.0` (enabled, vacuous) from
  `None` (feature off) — the distinguishability the old doc claimed without a
  field.
- `fully_gated` is the machine-readable answer to "fresh-but-unconfident vs.
  dead cache" (§3.6): a fresh `{}` with `fully_gated=True` is **not** the
  max_stale dead-cache `{}`.
- `pick_label` + `label_matched` preserve the unknown-label signal that
  gate-first ordering would otherwise swallow: a drifted label set is a
  contract problem, not a confidence problem, and the operator can now tell
  them apart (`fallback_reason="low_confidence"` + `label_matched=False`).

`JevStats` gains `gated: int = 0` — sensor increments by the number of
**blocked** questions per call; strategy increments by 1 per blocked pick.
Calibration tool, nothing more.

Logging: one `logger.info` per call (aggregated, bounded) naming blocked
questions with confidence and threshold, and listing pass-throughs with
absent confidence:

```
"Gating low-confidence judgments: blocked=['threat' (confidence=0.42 < threshold=0.80)]; absent_confidence=['proximity']"
```

Gating is expected behavior when enabled, so `info`, not `warning`. No
per-question log spam; the record carries the detail.

No new top-level exports beyond the additive telemetry types:
`GateDecision` is exported (it is part of the binding producer contract, §9);
gating itself remains parameters on existing classes. `goapauto/__init__.py`
`__all__` gains `GateDecision` only.

---

## 3. Precise semantics

### 3.1 What "confidence" means per question type

| Question | Effective confidence | Source |
|---|---|---|
| `Choice` | `answer.confidence` (model-reported) | `ChoiceAnswer.confidence` |
| `Score` | `answer.confidence` (model-reported) | `ScoreAnswer.confidence` |
| `Noul` | `None` — always | **Ruling C: gating does not apply to Noul judgments** |

**Ruling C (binding):** the decisiveness proxy `2·|noul−0.5|` is rejected — a
category error (a calibrated `0.6` is a confident probabilistic judgment).
Noul answers carry `confidence=None` (unknown); they **pass through
flagged** (recorded in `unknown_confidence_questions` /
`confidence_absent`, `GateDecision(reason="absent_confidence")`) and are
**never blocked**, whatever threshold is configured. Open limitation,
recorded honestly: Noul's uncertainty lives in the value itself; the gate
cannot see it. A caller that needs Noul judgments gated must do it from the
value downstream — this design offers no proxy.

Reported confidences are validated **in the Jev layer only**: `not 0.0 <=
conf <= 1.0` (this single comparison also catches `NaN`) raises
`TypeSafeError` — corrupt judgment data fails loudly instead of silently
passing the gate. Clamping was considered and rejected for the Jev layer.
**Layer boundary (Ruling D):** the generic `JudgmentSensor`/`JudgmentStrategy`
layer (doc 05) raises `JudgmentError` on the same condition; the `JevJudge`
adapter translates at the boundary (Jev-native errors surface as
`TypeSafeError` to Jev callers, `JudgmentError` to generic callers). This
doc governs the Jev layer: `TypeSafeError`.

### 3.2 The gate rule

For a question with effective threshold `t` (possibly `None`) and effective
confidence `c`:

- **`t is None` → gating off for this question. The threshold check runs
  FIRST; when it is `None`, `_answer_confidence` is never called** — no
  attribute read, no validation, no float ops. Zero overhead, zero behavior
  change: a backend emitting `NaN` confidence passes silently exactly as in
  0.5.0. (This is the byte-identical guarantee, stated once.)
- `c is None` (Noul, or a backend that reported no confidence) →
  **pass-through**, recorded as absent (`unknown_confidence_questions` /
  `confidence_absent`, `reason="absent_confidence"`). Absent confidence is
  never conflated with `0.0` (would block everything) nor with `1.0` (would
  destroy the confident/unknown distinction): failing open on the unknowable,
  loudly.
- Otherwise **gate iff `c < t`** (strictly less). The boundary is inclusive:
  `c == t` passes, in float arithmetic as computed — no exact-decimal
  guarantee is promised across JSON serialization. `t = 0.0` therefore gates
  nothing (enabled but vacuous — distinguishable from `None` via
  `gating_enabled=True` in the record); `t = 1.0` gates everything except
  `c == 1.0` exactly (e.g. `FakeTypeSafeClient`'s canned
  `ChoiceAnswer(confidence=1.0)` passes a `1.0` threshold — useful in tests).
- **The vacuous-but-validating corner, stated explicitly:** validation is tied
  to *enabled-ness* (`t is not None`), not to whether anything can gate. With
  `t = 0.0`, a `NaN` confidence raises `TypeSafeError` even though nothing
  gates. That is fail-loud by design, not an accident.

### 3.3 Sensor extraction with gating

`_extract` returns the gated updates plus gate metadata. Gating is a **filter
on the updates dict**; the cache layer keeps prior entries for keys absent
from the dict (per-key merge, Ruling A1):

```python
def _extract(self, response) -> tuple[dict[str, Any], _GateInfo]:
    updates, decisions = {}, []
    threshold_map = ...  # effective threshold per extracted question name
    for question_name, state_key in self._mapping.items():
        question = self._questions[question_name]
        threshold = threshold_map.get(question_name)   # None = off for this question
        answer = ...   # via response.nouls / .choices / .scores as today
        value = ...    # .noul / .choice / .score as today
        if threshold is None:
            updates[state_key] = value                  # byte-identical path: no confidence access
            continue
        conf = _answer_confidence(question, answer)    # §3.1; raises TypeSafeError on corrupt values
        if conf is None:
            decisions.append(GateDecision(question_name, state_key, None, threshold,
                                          "proceed", "absent_confidence", None))
            updates[state_key] = value                 # pass-through, flagged
        elif conf < threshold:
            decisions.append(GateDecision(question_name, state_key, conf, threshold,
                                          "blocked", "low_confidence", "drop"))
            # DROP the update: key absent from this cycle's dict
        else:
            decisions.append(GateDecision(question_name, state_key, conf, threshold,
                                          "proceed", "passed", None))
            updates[state_key] = value
    return updates, _GateInfo(decisions=tuple(decisions))
```

In `_judge`, on success: gated keys are **not written**; the cache keeps
their previous entries **with their previous age** — gate-kept values do NOT
refresh the staleness clock (Ruling A1, stated explicitly). Concretely:

- 0.5.0 cache shape: merge per key — `self._cached` keeps old entries for
  keys absent from `updates`; only present keys are replaced.
- 02 cache shape: the wrapped sensor's returned dict already omits gated
  keys; 02's own rule ("keys absent from the wrapped output keep their old
  value under the ladder") does the right thing with no 04-side deletion.
- `last_success` / `last_call` advance (the model answered; perception
  contact is fresh) — but per-key age of gate-kept keys is untouched, so 02's
  ladder keeps aging them honestly toward STALE/DEAD. The kept value is old
  data and its age is now *visible*, not discarded.

Telemetry records `gate_decisions`, `gated_questions` (blocked names),
`unknown_confidence_questions`, `gating_enabled=True`,
`fully_gated=(gating on and every extracted question blocked)`.

A fully-gated call returns `{}` for that cycle — **not an error**. The stale
path on a later `TypeSafeError` replays only cached judgments, all of which
**passed the gate when cached** (a stale cached judgment that "would also
gate" today cannot exist — and it is never evicted by a gated write). The
reviewer's scenario is impossible by construction: one low-confidence blip
keeps the previous high-confidence entry and its age; one network blip then
replays that entry, not `{}`.

### 3.4 Strategy selection with gating

```python
def select(self, goals, state):
    ...  # build choice question as today
    threshold = self._confidence_threshold
    pick_answer = response.choices["goal"]
    pick_label = pick_answer.choice
    conf = None
    if threshold is not None:
        conf = _answer_confidence(choice_question, pick_answer)  # validated Choice confidence
    if threshold is not None and conf is not None and conf < threshold:
        record(GateDecision("goal", None, conf, threshold, "blocked", "low_confidence",
                            self._fallback_policy))
        return self._fallback(goals, state, reason="low_confidence",
                              pick_label=pick_label, label_matched=...)  # label match still computed for telemetry
    if threshold is not None:
        record(GateDecision("goal", None, conf, threshold, "proceed",
                            "passed" if conf is not None else "absent_confidence", None))
    for label, goal in labeled:          # label matching as today
        if label == pick_label:
            return goal
    return self._fallback(goals, state, reason="unknown_label",
                          pick_label=pick_label, label_matched=False)
```

The gate check runs **before** label matching: a low-confidence pick is
untrusted regardless of whether its label is known. Label matching is still
computed on the gate path **for telemetry only** (`pick_label`,
`label_matched`), so an operator can distinguish "untrusted pick" from
"drifted label set" — the unknown-label signal is preserved, not swallowed.

All three fallback triggers share one helper; the recorded reason always
distinguishes them:

```python
def _fallback(self, goals, state, reason, pick_label=None, label_matched=True):
    # reason: "error" | "unknown_label" | "low_confidence"  (never combined; label facts ride alongside)
    # telemetry: error name when reason == "error";
    #            gated=True, gate_decisions=[blocked...] when reason == "low_confidence";
    #            pick_label + label_matched recorded in all three cases
    # log: warning with reason (keeps today's log lines for error/unknown_label)
    if self._fallback_policy == "priority":
        return PriorityGoalStrategy().select(goals, state)  # delegation, not duplication
    return goals[0]
```

**Import-cycle verification (MUST FIX 2):** `goal_arbitrator.py` imports only
`goal` and `worldstate`; nothing in it imports `jev`. Adding
`from goapauto.models.goal_arbitrator import PriorityGoalStrategy` to
`jev.py` therefore creates no cycle (verified against the 0.5.0 sources).
Delegation also fixes the tie-break question for free: `min` is stable, so
ties resolve to the earliest goal in list order — identical to the old
inline rule. Keeping `fallback_policy` (with `"priority"` as a thin
delegation) is the coordinator's direction; the red-team's "cut it"
recommendation is answered in §8.

This answers the brief's question directly: **yes, error fallback and
low-confidence fallback share machinery** — one `_fallback` with a recorded
reason. The *difference* is policy, not plumbing: on error the model said
nothing usable (keep today's `"first"` default to stay byte-identical);
on low confidence the model spoke but shouldn't be trusted, so the
principled fallback is the developer's static priority order — available via
`fallback_policy="priority"` for both paths, default `"first"` so nothing
changes unless asked.

### 3.5 Edge cases

- **Empty goals list** → `None`, before any model call (unchanged).
- **Threshold set, model call errors** → error path, not the gate path.
  Telemetry: `error` set, `gated=False`, `fallback_reason="error"`,
  `gate_decisions=()` (the gate was never consulted).
- **Unknown label + would-also-gate** → `fallback_reason="low_confidence"`
  (gate runs first; the label is untrusted anyway), but `pick_label` and
  `label_matched=False` are recorded so the contract problem stays visible.
- **`confidence_thresholds={}` with global set** → global applies to all.
- **Noul exactly `0.5`** → confidence `None` → passes through flagged,
  whatever the threshold (Ruling C; the old "model abstains → gates" behavior
  is gone).
- **`t = 0.0` + `NaN` confidence** → `TypeSafeError` (vacuous-but-validating
  corner, §3.2 — fail-loud by design).
- **`FakeTypeSafeClient`**: canned values build
  `ChoiceAnswer(confidence=1.0)` / `ScoreAnswer(confidence=1.0)`; `NoulAnswer`
  has no confidence field → `None` path. Pre-built answers (e.g.
  `ChoiceAnswer(choice=..., confidence=0.2, probabilities=...)`) give tests
  full control of the gate.
- **Fully-gated call followed by a forced error** → the stale path replays
  the *kept* prior entries (gated keys were never evicted, their age kept
  aging), not `{}` — unless those entries are themselves past `max_stale`,
  in which case `{}` as today.

### 3.6 Planner-loop guidance (guidance note, NOT new machinery)

When `sense()` returns `{}` the host must determine *which* `{}` it is before
feeding 01's `sensor_age`:

- `record.fully_gated` **True** → perception is **fresh**; the model
  answered and every judgment was blocked. The host must **NOT** apply 01's
  dead-cache guidance (inflating `sensor_age` to suppress replans) — that
  guidance is for judgments older than `max_stale`. Treating fresh-but-
  unconfident perception as dead sensors is the signal-conflation bug the
  review caught.
- `record.fully_gated` **False** and `stale_cache_hit` **True** past
  `max_stale` → the dead-cache `{}`; 01's `sensor_age` guidance applies.

What the host *should* do on a fully-gated `{}` is a host policy decision,
not library machinery. Sensible options: keep the current plan (the world
state still holds the last confident values, aging honestly under 02), or
feed the low-confidence signal into 01's `min_plan_confidence`-style forced
replan. What it must not do is conflate the two `{}`s. The distinguishing
channel is the telemetry record (`fully_gated`, `gate_decisions`) and the
sensor's `diagnostics()` mapping (§5e) — **not** the shape of the returned
dict, which is identical by contract.

---

## 4. Failure-mode table

| Failure mode | Handling |
|---|---|
| Threshold outside `[0.0, 1.0]` at construction | `ValueError` immediately (fail-fast) |
| `confidence_thresholds` names a question not in the effective `mapping` | `TypeSafeError` at construction (dead config is fail-loud) |
| `confidence_thresholds` value is `None` | `ValueError` at construction (no per-question opt-out entries) |
| All sensor questions block on a fresh call | Return `{}`; cache keeps prior entries with prior ages; `last_success` advances; telemetry `fully_gated=True`, `gate_decisions` lists every block; **not** an error |
| Gated write followed by `TypeSafeError` | Stale path replays kept prior entries (never evicted); beyond `max_stale` → `{}` as today |
| Strategy pick below threshold | `_fallback(..., "low_confidence")`; telemetry `gated=True`, `fallback_reason="low_confidence"`, `pick_label` + `label_matched` recorded |
| Backend reports `NaN` or out-of-range confidence (Jev layer) | `TypeSafeError` raised — corrupt judgment data, fail loud (not clamped, not passed). Generic layer (doc 05): `JudgmentError`; adapter translates at the boundary |
| Backend reports no confidence (incl. all Noul) | Pass-through + flagged (`unknown_confidence_questions` / `confidence_absent`, `reason="absent_confidence"`); never treated as `0.0` or `1.0` |
| Gating enabled but model unreachable | Error path as today; gate never consulted (`gate_decisions=()`) |
| Caller passes `threshold=0.0` vs `None` | `0.0` = enabled-but-vacuous (`gating_enabled=True`, proceed decisions recorded, nothing blocks); `None` = feature off (zero overhead, zero confidence access, `gate_decisions=()`); distinguishable in the record |
| Gating fully off (`None` everywhere) | Byte-identical to 0.5.0: no confidence attribute read, no validation, no telemetry fields populated |

---

## 5. Interaction with the other five differentiators

**(a) Runtime / replan budgets.** Gating does **not** consult cost or latency
and performs no additional model calls. It runs strictly after the answer
arrives, on data already paid for. A gated judgment still counts as a
completed call for budget accounting (it consumed the budget; the gate only
decides whether the answer reaches the planner). No re-query, no escalation —
those would make `sense()` latency unpredictable and are non-goals (§1).
**Scope disclaimer (attack 13):** 01 §5c's `plan_confidence` scorer is not
produced by this design — 04 gates per-answer judgments, never whole plans.
The name collision is 01's to fix (its §5c should stop attributing plan
scoring to "confidence gating (design 04)"); the scorer itself belongs to a
future design.

**(b) Sensor caching / staleness.** Composition rule (Ruling A, binding):
**CONJUNCTION — gate at write, replay at read.**
- A fresh call's answers are gated once, at extraction; gating is a filter
  on the updates dict. The cache merge keeps prior entries for gated
  (absent) keys **with their prior age** — gate-kept values do NOT refresh
  the staleness clock (Ruling A1, §3.3).
- The stale-error path never re-gates: cached judgments already passed the
  gate when written. A stale cached judgment that "would also gate" today
  cannot exist — it would never have been cached, and a gated write can
  never evict it.
- **Staleness does NOT decay confidence** (the multiplicative
  `staleness_discount` composition is rejected — 02's reviser deletes it):
  confidence is a property of the judgment at answer time; aging is the
  staleness design's axis (`max_stale`); uncertainty is gating's axis. A
  judgment reaches the world state iff it was confident-enough when made
  AND fresh-enough when replayed. Independent axes, both gates must pass.
- Thresholds are constructor-fixed, so there is no "threshold changed
  between write and read" case. (If a future design adds dynamic thresholds,
  re-gating cached values at read time must be re-decided then.)

**(c) Action interruption.** A gated mid-plan judgment does **not** interrupt
execution. Gating affects only what `sense()`/`judge()` return — i.e. the
next sense/arbitrate cycle's inputs. Mid-plan interruption policy belongs to
the interruption design; this design is perception-boundary only and emits
no interruption signal.

**(d) Provider-independent judgment interface.** Confidence **must** be
`float | None` in the judgment protocol — `None` meaning "this backend
cannot produce confidence." Gating's contract with the protocol:
- `None` → pass-through + flagged (§3.2), never blocked, never coerced to
  `0.0` **or** `1.0`. The 05 review's own fix adopts this framing over 05's
  "treat-absent-as-1.0" recommendation — 04's pass-through+flagged is the
  agreed semantic; 05's reviser makes it normative. (Treating absent as 1.0
  destroys the confident/unknown distinction; treating it as 0.0 was never
  proposed by 04.)
- Non-`None` → validated to `[0,1]` in the Jev layer (`TypeSafeError`
  otherwise), then gated per §3.2. **Layer boundary:** the generic layer
  validates-and-raises `JudgmentError` (cheap, fail-loud, no double-clamping
  — the 05 review's fix, consistent with Ruling D); the `JevJudge` adapter
  translates backend-native errors at the boundary. "Adapters SHOULD
  normalize; generic layers VERIFY; the Jev layer raises `TypeSafeError`."
  04's old open Q2 is resolved by Ruling D, not left open.
- Noul: per Ruling C the Jev adapter reports `None` for Noul confidence —
  no derived proxy anywhere. (05's "map to `None` when absent" and 04 now
  agree.)
- No generic `ConfidenceGate` class is built now: the shared logic is the
  internal `_answer_confidence(question, answer)` helper in `jev.py`, used by
  both classes, callable only after the threshold check. If the
  provider-interface design later needs gating outside Jev, that helper is
  the extraction point — YAGNI until then.

**(e) Why-diagnostics.** 04 keeps its pull architecture — **no separate event
bus, no push call into an inspector** (§8 attack 4 for the full negotiation).
The binding contract 06 builds against is the record-embedded trace (§9):
every gated decision (blocked AND proceed) is a `GateDecision` on the
`JevCallRecord`, with the coordinated event name and vocabulary. The 06
reviser ingests `record.gate_decisions` (duck-typed) and translates each into
a `judgment_gated` trace event. Additionally, `JevSensor` exposes a
duck-typed `diagnostics() -> Mapping[str, Any]` (additive, no behavior
change) for hosts that need queryable state rather than the per-call record:

```python
{
    "gating_enabled": bool,
    "effective_thresholds": {question_name: float},  # only questions with a threshold
    "gated_questions_last_call": [...],
    "fully_gated_last_call": bool,
    "gated_total": int,                              # mirrors JevStats.gated
}
```

This is the answer to "telemetry is fire-and-forget, not queryable state":
the record is the per-call event; `diagnostics()` is the queryable summary.

---

## 6. Test plan sketch (100% coverage, no `pragma: no cover`)

New branches and how each is covered (all via `FakeTypeSafeClient`, incl.
pre-built low-confidence answers):

- **Construction validation:** threshold `-0.1` / `1.1` → `ValueError`
  (sensor and strategy); `confidence_thresholds={"nope": 0.5}` →
  `TypeSafeError`; `confidence_thresholds={"threat": None}` → `ValueError`;
  threshold naming an asked-but-unmapped question → `TypeSafeError`.
- **Confidence derivation:** Noul → `None` (never blocks, flagged);
  Choice/Score use reported confidence; `NaN` and `1.5` reported confidence
  → `TypeSafeError` **only when gating enabled**; `NaN` with gating off
  passes silently (byte-identical regression — pins the hoisted check).
- **Gate boundary:** confidence exactly equal to threshold passes; epsilon
  below blocks. `threshold=0.0` blocks nothing, records
  `gating_enabled=True` with proceed decisions; `threshold=None` leaves
  `gate_decisions=()` and reads no confidence attribute (assert via a stub
  answer whose `confidence` property raises if touched).
- **Sensor:** below-threshold question dropped while others pass; returned
  dict lacks the key; **cache merge keeps the prior entry and its age** for
  the gated key (second `sense()` after a forced error replays the kept
  value, not `{}`); all-questions-block → `{}` with `fully_gated=True` and
  `last_success` advanced; a subsequent forced error replays the *kept*
  prior entries; `JevCallRecord.gate_decisions` contains proceed AND blocked
  entries; `JevStats.gated` counts blocks only.
- **Strategy:** low-confidence pick → `goals[0]` with default policy,
  highest-priority goal with `"priority"` (delegated; ties → list order);
  error path unchanged (`fallback_reason="error"`, `gate_decisions=()`);
  unknown label unchanged (`fallback_reason="unknown_label"`);
  low-confidence + unknown label → `"low_confidence"` with
  `pick_label` set and `label_matched=False`; `gated=True` only on the gate
  path; `confidence=threshold` passes and the pick is honored.
- **Absent confidence:** Noul answer → passes through, appears in
  `unknown_confidence_questions`, `GateDecision(reason="absent_confidence")`;
  stub answer with `confidence=None` on a Choice → same treatment.
- **Proceed visibility:** with gating enabled and all confidences high,
  `gate_decisions` records one `"proceed"` per extracted question — pins the
  "no lie by omission" requirement.
- **diagnostics():** `gating_enabled` False by default; True with thresholds;
  `effective_thresholds` matches constructor args.
- **Defaults unchanged:** existing test suite passes unmodified — gating off
  means byte-identical behavior, verified by the current 100%-coverage suite
  running green with no test changes.

---

## 7. Open questions

1. **Default `fallback_policy` for the low-confidence path.** This design
   keeps `"first"` as the default for zero behavior change, with
   `"priority"` opt-in. But the inherited "first goal on error" was never a
   designed choice — is `goals[0]` actually what Soulscape wants on *any*
   fallback, or should the product default become `"priority"`? (Kept open;
   needs Soulscape data.)
2. **Score confidence semantics.** The design assumes the TypeSafe server's
   `ScoreAnswer.confidence` is comparable to `ChoiceAnswer.confidence`. What
   the server actually puts there (model self-report vs. probability mass on
   the chosen level) should be verified against server behavior before anyone
   tunes production thresholds on Score questions. (Kept open; needs server
   evidence.)
3. **Who produces 01's `plan_confidence`?** Deferred — no owner among the six
   designs. A future design must define the per-plan scorer (aggregation of
   per-answer confidences? planner-internal?) before 01's `LOW_CONFIDENCE`
   replan has a producer.

Resolved in R2: Noul proxy (cut per Ruling C), clamp-vs-raise (raise per
Ruling D, scoped by layer), per-question `None` override (cut).

---

## 8. Adversarial review (red-team, 16 attacks — dispositioned)

Coordinator rulings referenced: **A** (conjunction composition), **A1**
(per-key eviction semantics; gate-kept values keep value + age, age never
refreshed), **C** (Noul confidence `None`, ungatable), **D** (Jev layer:
`TypeSafeError`; generic layer: `JudgmentError`; adapter translates),
**T** (threshold semantics: `None` = off with zero overhead; `0.0` = gate
nothing; gate iff `conf < threshold`).

1. **Whole-cache replacement evicts last-good judgments** → **RESOLVED**.
   §3.3 rewritten as a per-key merge per Ruling A1: gated keys keep their
   previous entries *and* their previous age; only passing keys refresh.
   The reviewer's blip-then-network-blip scenario now replays the kept
   high-confidence entry — impossible to lose it.
2. **02/04 composition contradiction** → **RESOLVED** by Ruling A:
   conjunction wins; no multiplicative decay; 02's `staleness_discount` is
   deleted by the 02 reviser. §5b states the single composition rule.
3. **Noul decisiveness proxy is a category error** → **RESOLVED** by Ruling
   C: proxy cut; Noul answers carry `confidence=None`, pass through
   flagged, never blocked. The limitation is recorded openly (§3.1).
4. **06's binding producer contract unimplemented/contradicted** →
   **RESOLVED by negotiation** (see §9): 04 adopts the *vocabulary*
   (event name `judgment_gated`, field names, `decision`/`reason`
   vocabulary) as a **record-embedded trace** (`GateDecision` on
   `JevCallRecord`), including **proceed** decisions — the "lie by omission"
   is fixed. 04 **rejects** the *push mechanics* (`record_judgment_gated`
   call, detached-inspector `ValueError`): the gating worker never holds an
   inspector reference; 06 ingests the record (pull), consistent with 04's
   long-standing "no separate event bus" architecture and with the 06
   review's own recommendation to cut the push API. 04 never emits
   `"escalated"` (non-goal §1.2); 06 must not require it. 06's open Q7 is
   answered: no inspector reference is held, so the detached-inspector
   question does not arise in 04 — the 06 reviser should replace §6d's push
   mechanics with pull-ingest of `gate_decisions`.
5. **NaN handling contradicts 05's adapter normalization** → **RESOLVED** by
   Ruling D: Jev layer raises `TypeSafeError` (fail-loud); generic layer
   validates-and-raises `JudgmentError`; adapter translates at the boundary.
   "Adapters SHOULD normalize; generic layers VERIFY." 04's old open Q2 is
   closed, not punted.
6. **Pseudocode violates byte-identical / zero-overhead** → **RESOLVED**:
   the threshold check is hoisted above all confidence access in both
   sketches (§3.3, §3.4). With gating off, no attribute is read, no
   validation runs, no decision is recorded. Pinned by a regression test
   (NaN confidence + gating off → silent pass).
7. **Fully-gated `{}` indistinguishable from dead-cache `{}`** →
   **RESOLVED**: `fully_gated: bool` on the record + `gate_decisions` +
   `diagnostics()` make the gated-empty case machine-distinguishable, and
   §3.6 documents what the planner loop must do (never apply 01's dead-cache
   `sensor_age` inflation to a fresh gated `{}`). Guidance note, not new
   machinery.
8. **Gate-kept values' age discarded** → **RESOLVED** by Ruling A1 (§3.3):
   gated keys keep `(value, as_of)`; per-key age is never refreshed by a
   gated write; 02's ladder ages them honestly.
9. **Gate-before-label-match swallows the unknown-label signal; pick never
   recorded** → **RESOLVED**: gate-first ordering kept (an untrusted pick is
   untrusted regardless of label), but `pick_label` and `label_matched` are
   recorded on every fallback path, so `low_confidence` + `label_matched=False`
   reads as "check your label set," not "tune your threshold."
10. **`0.0`-vs-`None` distinguishability claim was false** → **RESOLVED**:
    `gating_enabled: bool` on the record makes it true and testable
    (`0.0` → enabled, vacuous, proceed decisions recorded; `None` → off,
    nothing recorded). The vacuous-but-validating corner (NaN at `t=0.0`
    raises) is stated explicitly as fail-loud-by-design.
11. **Inclusive boundary is float fiction for derived confidences** →
    **RESOLVED**: the derived case is gone (attack 3); the boundary rule
    `c < t` blocks / `c == t` passes is stated once (§3.2) with the honest
    caveat that float equality is as-computed, no exact-decimal promise
    across serialization.
12. **Absent-confidence semantics: 05 "treat as 1.0" vs 04 "pass through
    flagged"** → **RESOLVED**: 04's pass-through + flagged is the agreed
    semantic (the 05 review's own fix adopts it; 05's reviser makes it
    normative). Absent is never coerced to `0.0` *or* `1.0`. Log spam
    bounded: one aggregated `info` line per call.
13. **01's `plan_confidence` has no producer** → **RESOLVED as a scope
    disclaimer**: 04 does not score plans; §1 non-goal 7 and §5a name the
    collision and defer the scorer to a future design (open question 3).
    01's §5c attribution is 01's to fix.
14. **Per-question thresholds for mapping-absent questions are dead config**
    → **RESOLVED**: constructor validates threshold keys against the
    *effective mapping* (actually extracted questions); anything else raises
    `TypeSafeError`.
15. **Over-engineering cuts** → **PARTIALLY RESOLVED**: the per-question
    `None` override is **cut** (rejected at construction; precedence is now
    one unambiguous sentence). `fallback_policy` is **kept** per the
    coordinator's direction (MUST FIX 2) — but `"priority"` is now a
    delegation to `PriorityGoalStrategy`, not a duplicated `Literal`
    framework, and the no-cycle import is verified. No generic
    `ConfidenceGate` class: held (YAGNI stands).
16. **Two namespaces for one key; duplicated priority logic** →
    **RESOLVED**: question-name namespace kept (consistent with `mapping`),
    documented; the `TypeSafeError` message must name the question-name
    namespace. Priority logic deduplicated via the verified delegation.

Nothing silently dropped: every numbered attack is dispositioned above.

---

## 9. Producer contract (binding — the 06 reviser builds against this)

### 9.1 The `judgment_gated` trace shape

Coordinated event name: **`judgment_gated`**. The binding field vocabulary
is `GateDecision` (§2.3):

| Field | Type | Meaning |
|---|---|---|
| `question` | `str` | Question name (`"goal"` for the strategy pick) |
| `key` | `str \| None` | Mapped state key; `None` for the strategy pick |
| `confidence` | `float \| None` | Effective confidence (`None` = backend reported none; always `None` for Noul per Ruling C) |
| `threshold` | `float` | Effective threshold applied |
| `decision` | `"proceed" \| "blocked"` | Gate outcome |
| `reason` | `"passed" \| "low_confidence" \| "absent_confidence"` | Why |
| `fallback` | `"drop" \| "first" \| "priority" \| None` | Action taken on block; `None` on proceed |

Invariants (producer-guaranteed): `decision == "blocked"` iff
`reason == "low_confidence"`; `decision == "proceed"` iff `reason` in
`{"passed", "absent_confidence"}`; `fallback is None` iff
`decision == "proceed"`; `confidence` is `None` iff
`reason == "absent_confidence"`; `threshold` is always within `[0.0, 1.0]`;
`confidence`, when not `None`, is within `[0.0, 1.0]` (validated before the
decision is recorded).

Decisions are emitted **only when gating was consulted** (effective
threshold non-`None` for that question). Gating fully off ⇒
`gate_decisions == ()`, no confidence access, no validation — the
byte-identical path.

### 9.2 Extended `JevCallRecord` fields (all defaulted, appended in this order)

`gate_decisions: tuple[GateDecision, ...]`, `gated_questions:
tuple[str, ...]` (blocked sensor question names),
`unknown_confidence_questions: tuple[str, ...]`,
`gating_enabled: bool`, `fully_gated: bool` (sensor),
`gated: bool` (strategy), `confidence_absent: bool` (strategy),
`pick_label: str | None` (strategy), `label_matched: bool` (strategy),
`fallback_reason: "error" | "unknown_label" | "low_confidence" | None`
(strategy).

**Cross-doc merge note:** 02 appends `staleness: str = "fresh"` to the same
dataclass. To avoid the field-order collision both reviews flagged, 02's
`staleness` field is appended **after** 04's block above. The 02 reviser owns
this ordering note.

### 9.3 `JevStats.gated` counter semantics

`gated: int = 0`. Incremented **only on blocks**: sensor `+=` number of
blocked questions per successful call; strategy `+= 1` per blocked pick.
Never incremented on proceed, on absent-confidence pass-through, on the
error path, or when gating is off. It is a calibration counter, not an
event log — the event log is `gate_decisions`.

### 9.4 Consumption contract for the 06 reviser

- 06 ingests `record.gate_decisions` (duck-typed; the record is the event
  source — **no `record_judgment_gated` push call exists**).
- Mapping to 06's `judgment_gated` trace event: `question` ←
  `GateDecision.question`; `confidence` ← `.confidence`; `threshold` ←
  `.threshold`; `decision` ← `.decision` (`"proceed"`/`"blocked"` —
  06's `"flagged"` token, if kept, maps to `reason ==
  "absent_confidence"`; `"escalated"` is never emitted by 04 and must not
  be required); `context` ← `"sensor:<key>"` / `"goal"`; `backend`,
  `model`, `latency_ms`, `error` ← the enclosing `JevCallRecord`.
- 04 does **not** embed the judged *value* in the trace (verbosity/privacy;
  the value already travels in 06's `sensor_update.updates` and in 06's
  strategy payload). 06 joins on `key` / `pick_label` — the join is
  documented here, not papered over.
- `fully_gated=True` on a fresh record is 06's signal for "fresh but
  unconfident perception" — it must not be rendered as dead-cache.
- `JevSensor.diagnostics()` (§5e) is the queryable summary for hosts that
  cannot consume the per-call record; 06 may merge it into `sensor_update`
  payloads per its existing duck-typed contract.

### 9.5 What 04 explicitly does NOT provide (so 06 doesn't assume it)

No push API. No inspector reference held by the sensor or strategy. No
`ValueError` on detached inspectors (the situation cannot arise). No
`"escalated"` decisions. No per-question judged values in the trace. No
plan-level confidence (see §1 non-goal 7).
