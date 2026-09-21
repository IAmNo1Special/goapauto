# Red-team review: 06 — "Why?" Diagnostics (`AgentInspector`)

**Reviewer stance:** hostile. Every claim below was checked against the
source in `~/workspace/goapauto-work/src/goapauto/` and against the
Interaction-notes sections of designs 01–05. File/line references are to
the current worktree.

## Verdict: NEEDS-REWORK

The design's headline promise — *"this differentiator **consumes** the
other five"* (§1) — does not survive contact with the five producer docs.
On all five axes the "binding field schemas" (§6) demand fields, hooks,
or mechanisms the producers never agreed to, under names the producers
don't use. Separately, two of the design's core derivations are wrong
against actual code (`judgment_fresh` from `stats()` call deltas;
`stop_reason` as the budget signal), and the transparent-wrapper
mechanism breaks the object-identity guarantees a fail-loud library
depends on. These are structural defects, not polish: as specified, the
inspector cannot deliver the three "why" answers it advertises.

## The three strongest attacks

1. **§6(a) is fiction — budget/replan events can never enter the trace.**
   `attach()` never subscribes to `on_budget_exhausted`, `on_replan`, or
   `on_replan_skipped`; worse, 01's replan hooks live on `ReplanPolicy`
   (its own `register_hook`), an object `attach()` never receives — so
   they are structurally unreachable, not just unsubscribed. And 01's
   `on_budget_exhausted` fires with `(plan=None, stats=stats)` kwargs,
   while 06's binding schema demands
   `{budget_name, limit, consumed, unit, phase, goal, action}` — not one
   of those fields exists in the hook payload. The §6(a) correlation rule
   ("any `budget_exhausted`/`replan_throttled` event whose `tick`
   matches…") quantifies over events that cannot exist.
2. **`stop_reason` will be `None` exactly when it matters most.**
   No producer doc sets it — grep over 01–05 finds zero mentions. Doc 01
   chose a different channel: `stats.budget_exhausted = True` (a bool on
   `PlanStats`). Doc 06's `plan_failed` payload doesn't even include
   `budget_exhausted`, so on a budget-exhausted search the inspector
   records `stop_reason=None` *and drops the flag 01 actually provides*.
   The doc's own showcase line — `inspector.why_plan().stop_reason` (§2.7)
   — prints `None` in the primary diagnostic case ("why didn't it find a
   plan?"). §4's claim that worker (a) "extends the vocabulary (e.g.
   `time_budget`)" is unfounded: worker (a) already decided, and it
   decided otherwise.
3. **The staleness derivation is provably wrong against `JevSensor`.**
   §6(b): "`calls` delta > 0 ⇒ `judgment_fresh=True`". But
   `JevSensor._record_telemetry` (`models/jev.py`) does `self._calls += 1`
   on *every* attempt — including the failure path that serves the stale
   cache (`_judge` → `except TypeSafeError` → `_record_telemetry(…,
   stale=True)`). So a stale-cache serve records
   `judgment_fresh=True, stale_served=True` on the same event: the
   inspector labels a stale judgment "fresh" — the exact lie the
   staleness design exists to prevent. Compounding it, the `sensor_stale`
   payload `{sensor, age_ms, max_stale_ms, keys}` cannot be filled from
   `stats()` at all: age lives in the private `_last_success`, the bound
   in the private `_max_stale` — contradicting §6(b)'s own "without
   touching `JevSensor` internals".

---

## Numbered attacks

### 1. Budget/replan events are structurally unreachable (§6a)

**Flaw.** Three compounding gaps: (a) §2.1's `attach()` subscribes to
seven planner hooks — `on_budget_exhausted` is not among them, and
neither are `on_replan`/`on_replan_skipped`. (b) Even subscription
wouldn't help for replans: 01 §2.4 gives `ReplanPolicy` *its own*
`register_hook`; the hooks fire on the policy object, and `attach()`
takes only `(planner, arbitrator, sensor_manager)` — the policy object
never reaches the inspector. (c) Field schemas match nothing: 01 §2.3
fires `on_budget_exhausted` with `(plan=None, stats=stats)`; 06 demands
`{budget_name, limit, consumed, unit, phase, goal, action}`. 01's
`on_replan(decision=...)` passes a `ReplanDecision` *object*; 06 demands
flat `{reason, decision: "skipped"|"deferred", trigger,
last_replan_tick, cooldown_remaining_ms}` — where `decision` collides by
name with 01's object-typed kwarg but means a string, and
`last_replan_tick` is unfillable by the producer because ticks are
inspector-owned (§2.2).

**Why it matters.** Concrete scenario: a Soulscape tick blows its search
time budget. 01 fires `on_search_failed` (recorded as `plan_failed`) then
`on_budget_exhausted` (unrecorded — nobody subscribed). The developer
opens the "Why this plan" report: "Budget events" section honestly says
"no budget events recorded" — while the plan *did* die to a budget. The
inspector's flagship cross-worker story silently degrades to the one
thing it promised to explain.

**Fix or question.** Either (i) `attach()` accepts the budget
producer(s) explicitly (`replan_policy=...`, plus subscription to
`on_budget_exhausted`) with an adapter that maps 01's *actual* payloads
(`stats` object, `ReplanDecision.detail`) into trace fields — or
(ii) drop the claim and mark §6(a) "pending producer contract". The
designer must answer: *which object does the inspector subscribe to for
replan events, and what is the exact adapter from `ReplanDecision` to
the flat schema?* "The budget worker emits `budget_exhausted`" (§6a) is
not a mechanism — the worker doesn't know the inspector exists.

### 2. `stop_reason` stays `None` on budget exhaustion; the real flag is dropped

**Flaw.** §4 adds `PlanStats.stop_reason` and §4.2 asserts worker (a)
"extends the vocabulary (e.g. `time_budget`)". Verified: zero mentions
of `stop_reason` in docs 01–05. Doc 01 §3.1 instead sets
`self.stats.budget_exhausted = True` and deliberately documents
*why* it didn't extend the result/stats shape further ("`planner.stats.
budget_exhausted` is the structured, programmatic channel"). Meanwhile
06's `plan_found`/`plan_failed` payload schema (§3.1) has `stop_reason`
but no `budget_exhausted` field — so the inspector records the one
channel and discards the other.

**Why it matters.** The single most common "why no plan?" case in
production (deadline hit mid-search) yields `PlanDecision(stop_reason=
None, success=False)`. The Markdown report cannot distinguish "frontier
exhausted" from "time budget killed it" from "max_iterations" — the
distinction the whole §4 change was built to provide.

**Fix.** Either get worker (a)'s written agreement to set `stop_reason`
(and reconcile it with 01's no-two-sources-of-truth rule), or — less
invasive and more honest — have the inspector read the channel 01
*actually* provides: record `budget_exhausted: bool` from `stats` into
the `plan_failed` payload and derive the report text from it. Do not
ship a schema field whose only producer is a sentence in §6.

### 3. `judgment_fresh` derivation labels stale judgments "fresh" (§6b)

**Flaw.** As established in the top-3 summary: `calls` increments on the
stale-cache failure path, so the "delta > 0 ⇒ fresh" rule fires on
exactly the wrong event. Additionally: when resense is *skipped*
(within `min_interval`, fingerprint unchanged), no telemetry is recorded
at all — `calls` delta is 0, so the wrapper reports
`judgment_fresh=False, stale_served=False` while serving cached (possibly
aging) judgments with `data_age_ms=None`. The developer sees "not fresh,
not stale" — a third state the taxonomy can't express. And
`sensor_stale`'s `age_ms`/`max_stale_ms` require the privates
`_last_success`/`_max_stale`, contradicting "without touching JevSensor
internals".

**Why it matters.** A developer debugging "why did the agent act on old
perception?" gets a trace that says the stale serve was a *fresh*
judgment. That's worse than no trace: it's a confident wrong answer from
the diagnostic tool.

**Fix.** Stop deriving from `stats()` deltas. Both `JevSensor` and
`JevGoalStrategy` already accept a `telemetry: Callable[[JevCallRecord],
None]` callback — `JevCallRecord` carries per-call `error` and
`stale_cache_hit` *exactly*. The inspector should ingest telemetry
records (it can even pass its own callback through the wrapper or read
the user's), not re-derive them from lossy counters. For `age_ms`/
`max_stale_ms`, either add them to `JevStats` (additive, producer-owned)
or drop them from the binding schema. Sharp question for the designer:
*why was the telemetry callback — purpose-built for this — bypassed in
favor of counter diffing?*

### 4. Interruption: wrong hook name, wrong event name, wrong fields (§6c)

**Flaw.** 06 subscribes to `on_action_interrupted` "if the hook exists"
and defines TraceEvent type `action_interrupted` with binding fields
`{action, step_index, plan_length, reason, detail, state_diff}`. Doc 03
§5(e) already decided the signal: hook **`on_execution_interrupted`**,
event **`execution_interrupted`**, fields `{event, reason, source,
executed_actions, remaining_actions, next_action, state_diff,
interrupted_at}`. The names don't match (so "subscribe only if present"
subscribes to nothing — `register_hook` raises `ValueError` on unknown
names, but 06 checks `event in planner.hooks`, which will simply be
absent), and the field sets are disjoint except `reason`/`state_diff`:
`action`, `step_index`, `plan_length`, `detail` are unpromised;
`source`, `executed_actions`, `remaining_actions`, `next_action`,
`interrupted_at` are unconsumed.

**Why it matters.** 03 *guarantees* "the event exists, fires exactly once
per interrupted run" — and 06 will never see it, because it's listening
on the wrong name. An interrupted action is invisible in the trace while
both designs claim it's covered. Also note 06's open questions 7.5/7.6
("hook vs. exception is worker (c)'s call") — it isn't open; 03 decided
it. The designer didn't read the producer doc.

**Fix.** Adopt 03's names and fields verbatim: subscribe to
`on_execution_interrupted`, event type `execution_interrupted`, and map
03's actual kwargs. Derive `step_index`/`plan_length` only if 03 agrees
to provide them; otherwise drop them — do not synthesize. Also reconcile
with 03's deliberate choice that interruption is *not* routed through
`on_execution_failed` (06 §2.1 registers `on_execution_failed` with no
event type mapped to it — see attack 9).

### 5. `judgment_gated` push API contradicts 04's explicit pull contract

**Flaw.** 06 §6(d) requires the gating worker to call
`inspector.record_judgment_gated(...)` — a public push API, with the
worker "holding the inspector reference only when one is attached". Doc
04 §5(e) explicitly rejects this architecture: *"No separate event bus
is introduced by this design — the telemetry record *is* the event; the
diagnostics design should ingest its new fields."* 04's actual
telemetry: `JevCallRecord.gated_questions`,
`unknown_confidence_questions` (sensor); `gated`, `confidence_absent`,
`fallback_reason` (strategy); `JevStats.gated`; `logger.info` lines. 06
ingests *none* of these and instead demands per-judgment
`{backend, model, question, value, confidence, threshold, decision,
context, latency_ms, error}` — fields 04 never promises (`value`,
`threshold`, `context` don't exist in 04's telemetry). The `decision`
vocabulary `"proceed" | "blocked" | "flagged" | "escalated"` is invented:
04 never defines it, and "escalated" names an outcome 04 lists as a
**non-goal** ("No re-query, no escalation", §1/§5a). Meanwhile 04's real
`None`-confidence policy ("pass-through + flagged", §3.2/§5d) has no
mapping into 06's schema — what `decision` does a pass-through get?

**Why it matters.** Concrete scenario: gating blocks a low-confidence
`danger` judgment. 04 records it in `JevCallRecord.gated_questions`
and `JevStats.gated`. The inspector, waiting for a
`record_judgment_gated()` call that 04's design says will never come,
shows nothing. The developer's "why did it act on low confidence?"
question — the one 06 §6(d) calls out as the interesting debugging
question — is unanswerable from the trace.

**Fix.** Delete `record_judgment_gated` and ingest what 04 actually
emits: the telemetry callback records (the inspector can already see
them — see attack 3's fix) plus `JevStats.gated` deltas. If per-judgment
`value`/`threshold`/`context` are truly needed, that's a change request
to worker 04, not a unilateral binding schema. And the `ValueError` on
"never-attached inspector" (§6d/7.7) creates a construction-order tangle
— gating wrappers are built around sensors before any inspector exists —
that the pull model avoids entirely.

### 6. Backend naming contradicts 05's canonical vocabulary

**Flaw.** 06 §6(b) hardcodes `backend: "typesafe"` for `JevSensor`;
§6(e) duck-types `backend_name()` or
`f"{type(client).__module__}.{type(client).__name__"`. Doc 05 §6(e)
*already defined* the canonical names: `JudgmentResponse.backend`
(`"jev"`, `"fake"`, `"instructor-openai"`), passed through by the
adapter. So the same judgment would be `"typesafe"` in the inspector's
sensor events, `"jev"` in 05's records, and
`"typesafe_sdk.TypeSafeClient"` in 06's own `JevDecisionInfo.backend`
(§2.4 example). Three names for one backend in one trace.

**Why it matters.** `timeline(types=[...])` filtering and any downstream
`jq` over JSONL break when the backend field has three spellings. The
"provider-independent" story collapses at the naming layer.

**Fix.** Adopt 05's canonical `backend` strings now — 05 is written, the
names exist. Delete the `"typesafe"` hardcode and the module-qualified
fallback for Jev-backed components.

### 7. Transparent wrappers break identity in a fail-loud library

**Flaw.** `attach()` replaces `arbitrator.strategy` and every
`sensor_manager.sensors[i]` with recording decorators, restoring on
`detach()`. Consequences, all silent: (a) user code doing
`isinstance(arbitrator.strategy, JevGoalStrategy)` or
`type(s) is JevGoalStrategy` now fails — including *inside* goapauto
itself if any future code branches on sensor type; (b) a user subclass
overriding `select()`/`sense()` and calling `super()` still works, but
any override the wrapper doesn't explicitly forward (e.g.
`JevSensor.judge()`, `close()`, `last_report()` from 02's design) is
reachable only via `__getattr__` — and `close()` on a wrapper that is
later detached leaks or double-closes depending on ordering; (c) a
reference captured *before* attach (`s = arbitrator.strategy`) bypasses
recording entirely while planning proceeds — `why_goal()` goes stale
with no error; (d) `SensorManager.add_sensor()` *after* attach appends an
unwrapped sensor — silently unrecorded; (e) `GoalArbitrator` subclasses
overriding `select_goal` to not use `self.strategy` bypass the wrapper
with no warning.

**Why it matters.** This library's contract is "fail loudly instead of
being silently skipped" (`SensorManager.update_state` docstring;
`Planner` docstring: "broken hooks raise instead of returning an empty
result"). Monkeypatching user objects to *silently* change their
identity — with at least three silent-bypass paths (c–e) — is the
opposite of that contract, in the diagnostic tool of all places.

**Fix or question.** Either make the wrappers *loud*: verify on every
recorded call that the wrapper is still installed (`arbitrator.strategy
is self._wrapper`, else raise), reject `add_sensor` post-attach, and
document the `isinstance` breakage as a known cost — or drop object
swapping for an explicit model: the inspector exposes
`note_goal_selected(...)`/`note_sensor_update(...)` record APIs and the
*user* (or thin documented shims) calls them, consistent with the
already-user-owned `mark_tick`. Sharp question: *why is `mark_tick`
user-called but goal/sensor recording done by invisible mutation?*

### 8. Forgetting `mark_tick` doesn't just degrade — it lies

**Flaw.** §2.2 claims "if the user never ticks… everything else works".
False for two documented fields: (a) `PlanDecision.replan` — "True when
a plan event already exists this tick". Unticked, *every* event has
`tick=None`, so the second plan in the process lifetime — possibly days
later, a different goal entirely — reports `replan=True`. The heuristic
doesn't degrade; it inverts. (b) Goal↔plan correlation (§3.2) falls back
to "the most recent `goal_selected` at any tick" — in a loop that
arbitrates once and plans repeatedly (or plans without arbitrating),
plans get attributed to a stale goal with no marker. (c) The unticked
`budget_events` rule ("falls between the previous and current plan event
by `seq`") is untestable hand-waving.

**Why it matters.** The canonical Soulscape loop *will* forget ticks
during early integration — that's when the inspector is most needed —
and the report will assert replan relationships that never happened.

**Fix.** When `tick is None`, set `replan=None` (unknown) instead of
computing it, and mark heuristically-correlated goals explicitly
(`goal_correlated: bool`) so the Markdown report can say "goal
correlated heuristically — call `mark_tick` per loop iteration for
exact attribution". Never present a heuristic as a fact.

### 9. Two registered hooks have no event type; one failure makes two events

**Flaw.** §2.1 registers callbacks on `on_execution_complete` and
`on_execution_failed`, but the `EventType` Literal (§2.5) contains
neither `execution_complete` nor `execution_failed` — the mapping is
unspecified. Separately, the planner fires *both* `on_action_failed`
*and* `on_execution_failed` at every action failure
(`models/goap_planner.py` lines 353/356, 380/383, 451/454, 476/479),
then raises. So one failed action produces two hook firings; the
inspector's event stream will contain `action_failed` plus
*whatever* `on_execution_failed` maps to — double-counting one failure
unless the design says otherwise.

**Why it matters.** `timeline()` shows two events for one failure;
`to_markdown` must either dedupe (unspecified how) or confuse. And an
implementer facing "register on_execution_failed but no event type"
will invent a mapping — the 100%-coverage suite will then enshrine an
unspecified behavior.

**Fix.** Specify: `on_execution_complete` → no event (or a
`execution_completed` type — but then say so); `on_execution_failed`
following `on_action_failed` for the same action → suppress as
duplicate (record the action failure once, note `execution_failed:
true` in its payload). State the dedup rule explicitly.

### 10. `action_failed` reason taxonomy is undeliverable from hook kwargs

**Flaw.** §3.1 promises `reason: "precondition" | "handler_error"`,
"distinguished by which `execute_plan` call site the hook arguments
indicate — the recorder inspects whether the state had already been
handed to a handler". The four call sites (two logical × sync/async)
fire with *identical* kwargs: `action=action, state=current_state`.
At the precondition site `current_state` is pre-handler; at the
handler-error site the exception aborted before reassignment, so
`current_state` is *also* pre-handler. There is nothing in the kwargs to
inspect — the disambiguation is unimplementable as specified. The only
real fix is the §7.3 planner change (adding `reason=` to the hook calls),
which breaks every existing callback with signature `(action, state)`
— a backward-compat break the design flags but doesn't resolve.

**Why it matters.** A shipped `reason` field that is actually a guess
(or always `"unknown_action"`) poisons the exact debugging question it
exists for: "did my precondition lie, or did my handler throw?"

**Fix.** Either take the planner change (additive kwarg with a default
at the *callback* side is impossible — kwargs go to user callbacks;
instead fire `reason=` and document the break, or version-gate it), or
drop the distinction and record `reason: "unknown_action"` honestly…
no — better: record no `reason` at all until the hook carries it. Do not
ship a taxonomy the transport can't fill.

### 11. Planner raises mid-search → `why_plan()` answers with a stale plan

**Flaw.** The failure table (§5) doesn't cover it, but the planner is
fail-loud: "Planning errors propagate to the caller — invalid inputs,
broken predicates, failing providers, and broken hooks raise instead of
returning an empty result" (`models/goap_planner.py` docstring).
`on_search_failed` fires only on the *normal* no-plan path
(`_finalize_plan_generation`, line 749) — an exception mid-search fires
no hook. So: tick N plans fine; tick N+1's search raises (e.g. a broken
predicate). The trace has no marker for N+1; `why_plan()` returns tick
N's `PlanDecision` — presented without qualification as *the* plan
decision.

**Why it matters.** The developer asks "why is the agent standing
still?" and the inspector shows a healthy plan from the previous tick.
A diagnostic tool must never present absence of data as data.

**Fix.** The inspector can't hook an exception it never sees without a
planner change — so say so, and provide the user-side pattern: document
that `generate_plan` should be wrapped by the *user* (or offer
`inspector.plan_failed_note(error)` record API) — or add the additive
planner hook `on_search_error`. At minimum, `why_plan()` should carry
the `seq`/`ts` of the decision so the report can show its age.

### 12. `detach()` during in-flight execution is unspecified

**Flaw.** §2.1: "`detach()` removes all hook callbacks, unwraps strategy
and sensors." Unspecified: (a) *how* hook callbacks are removed — if by
`list.remove` while an in-flight `execute_plan` is iterating that list
in `_trigger_hook` (`for callback in self.hooks.get(event, [])`), the
iteration silently skips callbacks (no error, just dropped events);
(b) whether the recorder checks an "attached" flag — a wrapper reference
captured between attach and detach (attack 7c) keeps appending events
*after* detach, contradicting "only live recording stops"; (c)
`SensorManager.update_state` iterating `self.sensors` while detach
rebinds the list is safe (rebind, not mutate) — but only if the
implementation rebinds rather than mutating in place, which the design
doesn't say.

**Why it matters.** Detach-while-running is exactly what a developer
does when the inspector itself is suspected of perturbing behavior —
the moment when trace integrity matters most.

**Fix.** Specify: hook removal by *replacing* the callback list
(`planner.hooks[event] = [cb for cb in ... if cb is not recorder]`),
never in-place mutation during iteration; the recorder checks an
`_attached` flag on every record call and drops (or raises on) post-detach
writes — pick one and document it; `detach()` rebinds (not mutates) the
sensor list. Add a test: detach mid-`execute_plan` from inside a hook
callback.

### 13. Silent eviction, inconsistent loudness, unbounded Markdown

**Flaw.** Three related: (a) `max_events` eviction is the doc's own word
"silently" (§3.3 table) — detectability via `timeline()[0].seq > 1`
requires the user to know to look; there is no `dropped_events` counter
or API. At 60Hz with 5–15 events/tick, 4096 events is ~11–68 seconds of
history — a debugging session blows past it without noticing.
(b) Inconsistent loudness: an evicted *tick snapshot* raises `KeyError`
(fail-loud), but `TickDelta.events` — drawn from the same ring buffer
under an *independent* bound — can be silently partial when the ring
evicted events the snapshot window still covers. One struct, two
loudnesses. (c) `to_markdown()` "What changed (…else since attach)" can
render up to `max_events` events — output size is O(buffer), unbounded;
the §5 failure table has no row for export size.

**Why it matters.** A developer diffs "what changed since attach" after
a long run and gets a multi-megabyte Markdown string — or worse, a
quietly truncated event list presented as complete.

**Fix.** Add `dropped_events: int` to the inspector (monotonic count of
evicted events) and render "…N earlier events evicted" in both
`timeline()` metadata and the Markdown report. Bound the Markdown event
rendering (`max_report_events`, default e.g. 200, with a truncation
marker). Reconcile the two bounds: either key both off one retention
policy or document that `TickDelta.events` may be partial and say so in
the report.

### 14. `last_decision` staleness paths; unknown-label has no schema field

**Flaw.** §4: "`last_decision` — set on every `select()` call, success
and failure paths." Not every path: (a) `if not goals: return None`
returns before any assignment — a subsequent read sees the *previous*
call's decision; (b) the duplicate-labels `ValueError` raises before
the try block — same staleness. A user catching the `ValueError` and
reading `strategy.last_decision` gets a decision for a *different* goal
set. Separately, the §7 test plan requires "unknown-label path records
fallback with the label kept in payload" — but `JevDecisionInfo` has no
field for the unknown label (`choice` is `None` "when fallback fired").
The test plan demands what the schema cannot hold — an internal
contradiction.

**Why it matters.** Stale decision objects are the "wrong answer with
confidence" failure mode, in the component whose whole job is
explaining decisions.

**Fix.** Set `last_decision = None` (or a fresh "no decision" record) at
the top of `select()` before any early return/raise — then every path
after it overwrites. Add `raw_choice: str | None` to `JevDecisionInfo`
for the unknown-label case. Also note `probabilities: dict[str, float]`
is non-optional in the schema — specify what it holds on the error path
(`{}`), since `response` is undefined there.

---

## Constraint-violation check

| Constraint | Verdict | Notes |
|---|---|---|
| uv workflow | **Pass** | Nothing in the design violates it. |
| Python ≥ 3.12 | **Pass** | No <3.12 constructs proposed. |
| 100% coverage, no `pragma: no cover` | **Pass (with risk)** | The test plan (§7) is thorough and every branch cited looks coverable — including the `json_safe` `<unrepresentable>` guard (hostile test double) and the double-attach/double-detach paths. Risk: the *unspecified* behaviors (attacks 9, 12) will force implementer inventions that must then be covered; unspecified ≠ uncoverable, but it means the "100%" claim is being made about a design with holes. |
| ruff + mypy green | **Question** | Cannot verify from a doc, but flag: the strategy wrapper must satisfy the `GoalSelectionStrategy` Protocol and the sensor wrapper the `Sensor` ABC *under mypy* while delegating everything else via `__getattr__` — attribute-swap assignment (`arbitrator.strategy = wrapper`) typechecks only if the wrapper declares `select` explicitly. Also `EventType` as an exported `Literal` alias used in `timeline()` signature is fine. No predicted failure, but the wrapper typing needs a spike before approval. |
| No new required dependencies | **Pass** | stdlib + pydantic only, as specified. |
| Zero-cost when disabled | **Pass on the letter, caveat on the spirit** | Verified: with `capture_search_tree=False` (default), `attach()` never touches `on_node_expanded` — no per-node work on the hot search path (the visualizer's per-node `id(node)` dict insert exists only under explicit opt-in). "Before `attach()`" is genuinely zero-cost. Caveat: *enabled*-mode cost is unbounded by payload — every `sensor_update`/`goal_selected`/`action_*` event runs `json_safe` over the full payload at record time (entire `sense()` dict, full `Goal` objects with `target_state`s, whole `Action` objects per action event). No bound on `data` size is specified; at 60Hz with a chatty sensor this is a real per-tick tax the "zero-cost" framing obscures. Recommend a `max_payload_depth`/`max_payload_bytes` guard or documented warning. |
| Public API via `goapauto/__init__.py` `__all__` | **Pass (to be verified)** | The design commits to it (`AgentInspector`, `TraceEvent`, `EventType`); implementation review must check. |

## Interaction-consistency check (docs 01–05)

### 01 — Runtime / replan budgets
- **Agreements.** 01 fires `on_search_failed` before `on_budget_exhausted`
  on deadline expiry, so the inspector's `plan_failed` recording *will*
  fire for budget kills (via the existing hook). 01's fail-loud hook
  contract matches 06's recorder-propagates philosophy.
- **Contradictions.** (1) 06 never subscribes to `on_budget_exhausted`
  and *cannot* reach `on_replan`/`on_replan_skipped` — those live on
  `ReplanPolicy`, an object `attach()` never receives (attack 1).
  (2) 01's hook kwargs `(plan=None, stats=stats)` vs 06's binding
  `{budget_name, limit, consumed, unit, phase, goal, action}` — no field
  in common except via digging through `stats` (attack 1).
  (3) `stop_reason` vs `stats.budget_exhausted: bool` — two designs
  added two different fields to `PlanStats` without referencing each
  other; 01 explicitly reasoned *against* extending the shape further
  (attack 2). (4) `ReplanDecision` object vs 06's string-typed
  `decision: "skipped"|"deferred"` — name collision, type mismatch.
  **Net: §6(a) must be rewritten against 01's actual contract or
  dropped.**

### 02 — Sensor caching / staleness
- **Agreements.** 02 explicitly reserves the event vocabulary to 06
  ("the diagnostics design owns the event vocabulary") and publishes a
  moments→fields table for 06 to consume — the collaboration 06 claims
  exists here is real.
- **Contradictions.** (1) Field names and units diverge: 02 emits
  `latency_ms` / `age_seconds` / `staleness: "fresh"|"stale"` band;
  06 demands `duration_ms` / `data_age_ms` / `judgment_fresh`+`stale_served`
  booleans (attack 3). (2) 02 promises **no** `diagnostics()` method —
  06's `data_age_ms` source is invented (attack 3). (3) 02's
  resense-skipped moments (`since_last_call_s`, fingerprint-unchanged)
  have no 06 counterpart. (4) 06's stats-delta derivation is semantically
  wrong against the current `JevSensor` (attack 3) — and 02's planned
  refactor of `JevSensor` onto the per-key cache ladder may change
  `stats()` semantics further, breaking the derivation a second time.
  **Net: closest to agreement of the five, but the field-level contract
  needs a joint pass — 06 must consume 02's actual moment table.**

### 03 — Action interruption
- **Agreements.** Both agree interruption is not failure (03: "not routed
  through `on_execution_failed`"; 06 has a separate event type) and that
  every interruption must be traceable exactly once.
- **Contradictions.** Hook name (`on_execution_interrupted` vs 06's
  `on_action_interrupted`), event name (`execution_interrupted` vs
  `action_interrupted`), and field sets are disjoint except
  `reason`/`state_diff` (attack 4). 06's open questions 7.5/7.6 ask what
  03 already decided. **Net: 06 must adopt 03's names/fields verbatim;
  §6(c) as written records nothing.**

### 04 — Confidence gating
- **Agreements.** Both agree every gated judgment — including
  low-confidence *proceeds* — must be visible in the trace, and both
  handle `confidence=None` without coercing to 0.0.
- **Contradictions.** Architecture: 04 says telemetry-pull ("no separate
  event bus… the diagnostics design should ingest its new fields"); 06
  says push (`record_judgment_gated`) — 06 ignored 04's explicit
  instruction (attack 5). Schema: 04's fields (`gated_questions`,
  `gated`, `confidence_absent`, `fallback_reason`, `JevStats.gated`)
  have no ingestion path in 06; 06's fields (`value`, `threshold`,
  `context`) don't exist in 04. Vocabulary: 06's `"escalated"` decision
  names a 04 non-goal. **Net: §6(d) contradicts its producer; delete the
  push API and ingest 04's telemetry.**

### 05 — Provider-independent judgment interface
- **Agreements.** Both put `backend` (+`model` when known) and
  `latency_ms` on judgment-bearing events; 06's "adopt the canonical
  name when (e) lands" is the right instinct.
- **Contradictions.** (e) already landed in draft: canonical names are
  `"jev"`/`"fake"`/`"instructor-openai"` via `JudgmentResponse.backend` —
  06's `"typesafe"` hardcode (§6b) and module-qualified duck-typing
  (§6e) ignore them, producing up to three spellings of one backend in
  one trace (attack 6). 05's `JudgmentCallRecord` has no
  `probabilities`/`choice`/`fallback_used` — 06 covers these via its own
  `last_decision` additive, which is fine, but 06's `judgment_gated`
  schema requires `question` while 05 offers question *keys* only
  optionally ("to keep traces compact"). **Net: adopt 05's canonical
  backend names now; reconcile `question` cardinality.**

## Over-engineering: what to cut

1. **The transparent wrappers** (attack 7) — the riskiest mechanism in
   the design, and philosophically off-brand for a fail-loud library.
   Replace with telemetry-callback ingestion (attacks 3, 5 fixes) plus
   small explicit `record_*` APIs for non-Jev components. If wrappers
   survive, they need the loudness guarantees in attack 7's fix.
2. **The speculative event types** — `budget_exhausted`,
   `replan_throttled`, `action_interrupted`, `judgment_gated`,
   `sensor_stale` are binding schemas for producers that haven't agreed
   to emit them. Ship the core taxonomy first (`tick_boundary`,
   `goal_selected`, `plan_found`/`plan_failed`, `action_started`/
   `action_completed`/`action_failed`, `sensor_update`,
   `sensor_error`); promote the rest from "reserved" to "binding" only
   when the producer contract is signed.
3. **Embedded `SearchTreeVisualizer`** — `SearchTreeVisualizer` already
   exists and composes manually in one line
   (`planner.register_hook("on_node_expanded", viz.on_node_expanded)`).
   The `capture_search_tree` flag, `keep_search_graphs` retention, and
   `search_tree_mermaid()` delegation add ownership without adding
   capability. Cut; keep `search_summary` (and resolve the §1-lazy-pull
   vs §3.3-retained-copy contradiction — pick one).
4. **`keep_ticks=64` snapshot retention** — full `get_state()` dicts for
   a debugging affordance, with a documented in-place-mutation
   corruption hazard. `what_changed_since` needs at most the *previous*
   snapshot; store diffs at `mark_tick` time or keep last-N=2. The 64
   default is unjustified.
5. **`record_judgment_gated`** — contradicts 04 (attack 5); cut.
6. **Second JSON-safety dialect** — `json_safe` vs `jev._json_safe`
   render the same callable two ways (bare `__name__` string vs
   `{"__callable__": qualname}`; `str(value)` vs `{"__repr__": …}`).
   Either generalize one implementation or document why the trace and
   the SDK payload may disagree about a value's rendering.

## Minor inconsistencies (for the implementer, not verdict-driving)

- `to_jsonl` writes `{"seq","tick","type","ts","data"}` but drops
  `mono` — while `TraceEvent` carries it and §3.6 says mono is for
  durations. Include it or say why not.
- `to_jsonl` *appends* (§2.6) — calling it twice duplicates the buffer
  into the file. Name says export, behavior says append; pick one.
- `GoalDecision.strategy` is "class name of the deciding strategy" —
  with the wrapper installed, naive `type(...).__name__` yields the
  wrapper's name. Specify unwrapping.
- `PlanDecision.search_summary` keys (`nodes`, `edges`,
  `max_depth_reached`) rename `get_search_graph()["metadata"]` keys
  (`expanded_count`, `visited_count`, `max_depth_reached`) — gratuitous
  renaming across one call boundary; keep the source names.
- `plan_found` payload uses `execution_time_s` while `PlanStats` calls
  it `execution_time` — same gratuitous rename.
- `per_action_costs` matching by action name is ambiguous when the
  search graph contains two edges with the same action name (loops);
  specify the walk (goal node back via parents) instead of name
  matching.
- `attach()` after `detach()` with different `max_events` — unspecified
  whether the old buffer survives.

## Bottom line

The recording core (hooks → `TraceEvent` → ring buffer → queries →
Markdown/JSONL) is sound and the failure-mode table is honest work. But
the design's differentiator claim — *"a real inspector… **consumes**
the other five"* — is currently a set of unilateral schemas addressed
to producers who already answered differently. Fix the five producer
contracts first (attacks 1–6), replace the wrappers and the stats-delta
derivation with the telemetry ingestion the codebase already offers
(attacks 3, 5, 7), and close the unspecified behaviors (attacks 8, 9,
11, 12). Then it's an approve.
