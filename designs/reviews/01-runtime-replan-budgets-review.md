# Red-Team Review — Design 01: Runtime & Replan Budgets

**Reviewer stance:** hostile. **Target:** `designs/01-runtime-replan-budgets.md` (design only, Phase 1).
**Codebase checked against:** `src/goapauto/models/goap_planner.py` (v0.5.0),
`goal.py`, `worldstate.py`, `__init__.py`, and the Interaction-notes sections
of designs 02–06.

## Verdict: NEEDS-REWORK

The `time_budget` half is nearly shippable. The `ReplanPolicy` half has a
first-call crash on its primary adoption path, no circuit breaker on the
replan triggers it deliberately exempts from throttling, and its §5
"contracts" assign homework to sibling designs that never accepted it —
verified against each sibling doc below. These are structural, not cosmetic.

## The three strongest attacks

1. **The policy crashes (or lies) on first use with an externally-created
   plan** — `should_replan` before any `replan()` call diffs against a
   `None` snapshot (`WorldState.diff` raises `TypeError` on non-WorldState,
   verified in `worldstate.py:201-206`) and step 4 compares `goal != None`
   → spurious `GOAL_CHANGED`. There is no `adopt()`/`note_planned()`.
2. **No circuit breaker on unthrottled triggers** — `NO_PLAN`,
   `PLAN_INVALID`, `GOAL_CHANGED`, `LOW_CONFIDENCE` all bypass
   `min_replan_interval` by design, so an unsolvable goal or a
   persistently-false precondition burns a full `time_budget` replan every
   tick forever. Doc 06 already assumes a `max_replans_per_tick` budget this
   design never builds.
3. **§5 contracts are fiction** — (c) is told to "score the fresh
   `PlanResult`" but doc 04 has no plan scorer (its gating is per-answer,
   and 01's own step ordering makes the budget→0.0-confidence mapping
   unreachable since `NO_PLAN` short-circuits first); doc 02 expects 01 to
   consume `SensorReport`/staleness plus a per-call node budget 01 doesn't
   build; doc 06 expects TraceEvent-shaped `budget_exhausted` /
   `replan_throttled` with tick fields while 01 emits planner hooks with
   different payloads and no adapter is specified; doc 03's
   boundary-interruption cannot surface as `PLAN_INVALID` (an interrupted
   but unstarted next action is usually still applicable → `PLAN_VALID` →
   resume, the opposite of the asserted contract).

---

## Numbered attacks

### API soundness

**1. `should_replan` accepts `plan` as a bare list but the host holds a
`PlanResult`.**
The signature is `should_replan(state, goal, plan, next_index=0, ...)`.
Nothing stops the host passing the `PlanResult` it just got back. A
`PlanResult` is a NamedTuple, so `plan[next_index]` silently indexes the
*fields* (`plan[0]` is the plan list, `plan[1]` the message string) instead
of raising. `plan[0][0]`-style downstream code then operates on garbage.
*Scenario:* `decision = policy.should_replan(state, goal, result,
next_index=i)` — no error, wrong answer, host executes nonsense.
*Fix:* accept `Plan | PlanResult | None`, unwrap `PlanResult.plan`
explicitly, and `TypeError` otherwise. Fail-loud is this project's
philosophy; silent NamedTuple indexing is its opposite.

**2. `replan(state, goal, decision)` on a don't-replan decision is
undefined.**
§3.4 dispatches on `PLAN_INVALID` / `WORLD_CHANGED` / `GOAL_CHANGED` /
`NO_PLAN` / `LOW_CONFIDENCE` and says "re-checks nothing; it acts". For
`PLAN_VALID`, `GOAL_SATISFIED`, `THROTTLED`, `SENSORS_STALE` the behavior
is unspecified — silent no-op? accidental replan? The 100%-coverage gate
means the implementer must invent a branch here, and per Malcom's rule
("ask before you make any changes — never guess") the design must answer
it.
*Fix:* `replan()` with `decision.replan is False` raises `ValueError`.
That is the only fail-loud-consistent choice.

**3. `replan()` takes a `decision` computed from a possibly different
state.**
The host flow is `should_replan(state, ...)` then `replan(state, goal,
decision)`. Nothing pins the two `state` arguments to be identical. If
they differ, `state_snapshot` / `last_goal` bookkeeping describes a state
the decision was never made against, and the `continue_plan` prefix is
stripped against the wrong history.
*Question the designer must answer:* is `replan` required to receive the
same objects, and if so, should it verify identity/equality and raise on
mismatch?

**4. `ReplanDecision.detail` is a mutable dict inside a frozen dataclass,
and `ReplanReason`'s type is unspecified.**
Frozen + dict field means `hash(decision)` explodes at runtime; more
importantly, doc 06 needs every payload JSON-safe. If `ReplanReason` is a
plain `Enum`, serializing a decision requires a custom encoder the design
never mentions.
*Fix:* specify `class ReplanReason(str, Enum)` and state the JSON
contract for `detail` (it currently can hold arbitrary `Any`, including
the offending `Action` object — is `detail["action"]` a name string or an
`Action`? §3.4 step 3 writes `{"action": name}` but a future reader will
put the object in).

**5. The `get_action` helper's `goal` parameter contract is vague.**
Design shows `planner.get_action("pickup_key", state, goal)`; the private
`_get_action_by_name(action_name, state, goal=None)` takes an optional
goal. Is the public form's `goal` required or optional? And per-tick cost:
`should_replan` step 3 calls it on *every* tick, and it re-queries *every*
provider (`provide_actions(state, goal)`) until a name match. With a
static provider that's cheap; with a dynamic (e.g. Jev-backed) provider
it can be a model call per tick just to check one precondition.
*Fix:* make `goal` keyword-optional like the private method, and either
document that step 3 may cost a provider call per tick or resolve the
`Action` once at plan time and cache it on the decision.

**6. `time_budget=True` (bool) is silently accepted as 1 second.**
`isinstance(True, (int, float))` is `True` and `True <= 0` is `False`, so
the validation as specified accepts it. Same class of footgun as the
`"fast"` → `TypeError` case the design already handles.
*Fix:* reject `bool` explicitly in validation.

### Failure modes the design ignores

**7. No circuit breaker on repeated replan triggers (the big one).**
`NO_PLAN` "never blocks the first plan" — and never blocks the second,
third, or four-hundredth either. `PLAN_INVALID`, `GOAL_CHANGED`,
`LOW_CONFIDENCE` all bypass `min_replan_interval` deliberately ("throttling
it would only hide a livelock"). Consequence: a goal that is unsolvable
within `time_budget`, or a precondition that reads false every tick
(stale sensor, flapping key), causes a full-budget replan *every tick,
forever*. The throttle protects only the `WORLD_CHANGED` path — the one
trigger least likely to storm.
*Concrete scenario:* Soulscape tick at 10 Hz, `time_budget=0.05`. Goal
unreachable in 50 ms. Every tick: `should_replan` → `NO_PLAN` → full
50 ms search → `plan=None`. The agent burns its entire frame budget
planning and never acts, with no escalation, no backoff, no log beyond
the hook. That is the livelock the design claims to avoid.
*Fix:* a consecutive-failure counter with backoff (e.g. after N
consecutive `NO_PLAN`/`PLAN_INVALID` outcomes, force `THROTTLED`-style
cooldown or require the world to change first), or a
`max_replans_per_tick`-style guard — note doc 06's §6(a) already lists
`"replans_per_tick"` as an expected `budget_name`, so the sibling design
assumes this knob exists.

**8. `PLAN_INVALID` bypass + flapping precondition = replan storm, and the
design has no damping.**
Step 3 bypasses the interval because "a broken plan cannot execute".
True for one tick. But a precondition keyed on a noisy sensor flips
false→true→false across ticks; each false tick triggers a full replan,
each true tick resumes. The design's answer to oscillation is "fail-loud",
but a replan storm is not loud — it's silent throughput collapse.
*Question:* should `PLAN_INVALID` carry a consecutive-trigger count in
`detail` and escalate to throttled after K consecutive invalids? Doc 04's
open question 2 already worries about flapping confidence scorers
causing replan storms through `LOW_CONFIDENCE` — same disease, no
treatment in either doc.

**9. The deadline is unenforceable against a single slow expansion, and
the design's "accepted imprecision" swallows the headline use case.**
§3.1 admits a slow hook/provider/`async_apply` overruns the deadline and
"stops at the next loop top". In the async path, `await
action.async_apply(current_node.state)` runs *per expansion* — with a
dynamic provider that call can be a network round-trip. A "50 ms budget"
can therefore take seconds with no violation of the spec, and the only
signal is `execution_time >> time_budget` after the fact.
*Why it matters:* the stated goal is "stay inside a frame budget". A
budget that is advisory under exactly the conditions (slow providers)
where you need it most is a footgun with documentation.
*Fix (minimal):* also check the deadline after each `await
action.async_apply(...)` / `action.apply(...)` in the expansion loop, and
put `{limit, consumed}` (i.e. `time_budget` and actual elapsed) on the
`on_budget_exhausted` payload — doc 06's `budget_exhausted` event already
wants `{budget_name, limit, consumed, unit}`, so emit what the sibling
needs instead of hoping the inspector synthesizes it (it won't — 06 says
it never synthesizes).

**10. `execution_time` (wall-clock `time.time()`) vs deadline
(`time.monotonic()`): the test the design proposes mixes clocks.**
§6 asserts `stats.execution_time <= time_budget + slack`. `execution_time`
is `time.time()`-based (existing stat, unchanged); the deadline is
monotonic. Under an NTP step, `execution_time` can go negative or exceed
the bound spuriously — a flaky test by construction.
*Fix:* either measure the budget-adjacent elapsed with `monotonic()` into
a separate field, or drop the inequality assertion and assert on
`budget_exhausted` + message only.

**11. Validation order vs. observable side effects.**
"Validation is fail-loud, before any search work" — but as specified,
`generate_plan` calls `_print_header` (prints when `verbose=True`, the
default) and takes `start_time` before validation would run if the check
is placed after the existing preamble. A `ValueError` for
`time_budget=-1` should not print a planning banner first.
*Fix:* specify that `time_budget` validation happens before
`_print_header`, i.e. first line of the method.

**12. Inconsistent validation: `min_replan_interval < 0` → `ValueError`,
but negative `max_sensor_age` and negative policy `time_budget` are
unvalidated.**
`max_sensor_age=-1.0` means `sensor_age > -1.0` is always true → world-change
replans are *permanently* suppressed, silently. `ReplanPolicy(time_budget=-5)`
passes through to the planner and raises only at replan time — fail-late.
*Fix:* validate all three eagerly in `ReplanPolicy.__init__`.

### Edge cases

**13. First-call adoption is broken (crash + phantom trigger).**
This is the strongest single defect. The natural integration path —
Soulscape already has a planner and plans; it constructs a `ReplanPolicy`
around them — hits two bugs before any `replan()` call:
  - (a) `should_replan` step 7 evaluates `state.diff(self._state_snapshot)`
    with `_state_snapshot=None`. `WorldState.diff` raises `TypeError`
    for non-`WorldState` (`worldstate.py:201-206`, verified). Crash.
  - (b) Even if (a) is guarded, step 4 evaluates `goal != self._last_goal`
    with `_last_goal=None` → `True` → spurious `(True, GOAL_CHANGED)` on
    the first call, triggering a needless fresh `generate_plan` and
    clearing `executed`.
  `reset()` reintroduces both. The test plan has no "adopt externally
  created plan" case.
*Fix:* add an explicit adoption method (e.g.
`policy.note_planned(state, goal)` / `adopt_plan(...)`) that seeds
`state_snapshot`, `last_goal`, `last_plan_time`, and clears `executed`;
specify that `should_replan` before adoption raises `ValueError`
(fail-loud) rather than crashing in `diff` or hallucinating a goal
change. Guard step 7's `None` snapshot regardless.

**14. The "prefix reuse is safe" claim is false for cyclic/undoable
actions.**
§3.4: "`continue_plan` only strips a leading run that matches, so a
stale prefix can never drop needed steps." Counterexample: executed
`["toggle"]`; current state has the switch off; the fresh plan from the
current state is `["toggle", "press_button"]` because the toggle must be
re-applied. `continue_plan` strips the leading `"toggle"` (it matches
the executed prefix) and returns `["press_button"]` — dropping a needed
step. The strip is order-based, not state-based; in any domain with
cycles or toggle actions the guarantee fails. This is existing
`continue_plan` behavior, but 01 *relies* on the guarantee as the premise
for routing `PLAN_INVALID`/`WORLD_CHANGED` through `continue_plan`.
*Fix:* weaken the claim to "strips a leading run; correct only when the
fresh plan's prefix is the already-achieved prefix" and document the
cyclic-action hazard, or make `replan()` verify the stripped plan still
achieves the goal from the post-execution state.

**15. `THROTTLED` vs `SENSORS_STALE` precedence is unspecified.**
Step 6 (staleness gate) runs before step 7 (world-change + throttle). If
the world changed *and* the data is stale *and* the interval hasn't
elapsed, which reason wins — `SENSORS_STALE` or `THROTTLED`? The prose
lists the staleness outcome inside step 7's bullet list, suggesting the
throttle is evaluated first, but the numbered order says staleness is
evaluated first. The 100%-coverage gate forces a test for the combined
branch; the implementer will guess.
*Fix:* specify the precedence (recommendation: `THROTTLED` first — the
interval is the cheaper, more local fact — or justify staleness-first).

**16. `on_replan_skipped` fires inside `should_replan` — a "query" with
side effects.**
Calling `should_replan` twice (e.g. once to log, once to act) fires the
hook twice. Worse, a host that calls `should_replan` speculatively (to
preview) gets hook traffic for a decision it never acts on.
*Question:* should hook emission move to explicit `replan()` /
`skip()` methods, or is double-fire acceptable and documented?

### Backward compatibility

**17. `on_search_failed` now fires on budget exhaustion — a semantic
broadening of an existing hook.**
Previously `on_search_failed` meant "the search genuinely failed"
(frontier empty / iterations exhausted). Now it also fires on timeouts.
An existing hook that retries with larger `max_iterations` will now spin
on timeouts; an existing hook counting "unsolvable" will now count
"too slow" as unsolvable. The only discriminator is the new
`stats.budget_exhausted` flag, which existing users don't know about.
Strictly, the default path (no `time_budget`) is unchanged, so this
passes the letter of "default behavior unchanged" — but any user who
opts into `time_budget` *and* has a legacy `on_search_failed` hook gets
a behavior change in that hook they didn't ask for.
*Fix:* document the broadening prominently in the hook's docstring and
the migration notes, and consider firing `on_budget_exhausted` *instead
of* `on_search_failed` (not in addition) — or keep both but say so loudly.

**18. Hook payload asymmetry: `on_search_failed(stats=...)` vs
`on_budget_exhausted(plan=None, stats=...)`.**
One carries `plan=None`, the other doesn't. Doc 06's inspector records
`on_search_failed` payloads; the extra kwarg on the sibling event is a
needless inconsistency for consumers to special-case.
*Fix:* pick one shape.

**19. `planner.hooks` gains a new key.**
Any code iterating `planner.hooks` (doc 06's inspector does `event in
planner.hooks` at attach time — fine) sees `"on_budget_exhausted"`.
Zero behavioral cost when unregistered. Not a violation, recorded for
completeness.

Otherwise on §4 of the task: default `generate_plan` path is untouched
when `time_budget=None` (one extra `is not None` comparison per pop —
"no overhead" is overstated by nanoseconds, but bit-for-bit outputs
hold); `PlanStats` extension is a defaulted appended dataclass field;
`ReplanPolicy` is a new module, fully opt-in. No default-behavior change
found beyond attack 17.

### What they chose NOT to build

**20. Non-goal 2 ("no per-action execution timeouts") is stale relative
to doc 03.**
Its rationale — "timeouts need threads or cancellation, which the
library avoids by design" — is falsified by design 03's cooperative
interruption (`execution.interrupt(...)`, `interrupt_when` predicates at
action boundaries, no threads). With 03 in the tree, a tick-budget
watchdog that interrupts execution *is* feasible without threads, and doc
03's §5(a) explicitly invites the budget owner to wire it up. But 01
builds no execution budget and names no owner — so 03's invitation is
addressed to nobody.
*Verdict on the non-goal:* keep "no execution timeouts *in this
design*", but rewrite the rationale to point at 03's mechanism as the
sanctioned path and name who owns the tick budget (the host/Soulscape),
instead of claiming it's impossible.

**21. Non-goal 1 ("no game-loop runner") creates the vacuum the sibling
docs keep tripping over.**
Doc 02's §5(a) assumes a "budget loop" with a "cheap pre-plan check"
(`SensorManager.health()`); doc 06's `replan_throttled` wants
`last_replan_tick` / `cooldown_remaining_ms` — tick vocabulary 01 cannot
speak (it has `clock()` and `elapsed`, no tick ids). The policy is
*almost* a loop (called between actions, tracks `note_executed`) but has
no tick context, so every sibling invents its own.
*Force back into scope (small, not a runner):* a minimal tick context —
e.g. `should_replan(..., tick_id=None)` passed through into
`ReplanDecision.detail` and the `on_replan*` hooks — or explicitly reject
06's tick fields and make 06 derive them. Leaving it unowned guarantees
three dialects of "when did the replan happen".

**22. Non-goal 3 (no partial plans) is defensible, but the design gives
the host nothing to do on exhaustion.**
"Fall back, don't treat as unsolvable" — fall back to *what*? On a first
plan with a tight budget there is no previous plan; on a replan the old
plan was invalidated (that's why we replanned). The honest guidance is:
keep executing the old plan if the trigger was `WORLD_CHANGED`
throttled, idle safely if `NO_PLAN` — i.e. the fallback policy belongs
in the `ReplanPolicy` doc, not as a `...` in a user example. As written,
the most likely host behavior on `budget_exhausted` is "retry immediately
with a bigger budget", which is just the replan storm from attack 7 with
extra steps.

### Over-engineering

**23. The `ReplanPolicy` is doing the host's job; ship the primitive,
not the policy.**
Decompose the value: (a) `time_budget` + `budget_exhausted` +
`on_budget_exhausted` — the genuinely load-bearing primitive, maybe 40
lines, and the only part Soulscape's tick loop strictly needs; (b) a
pure function `next_action_valid(planner, state, plan, next_index) ->
str | None` (returns the offending action name or `None`) — the other
half of the value, ~15 lines, no state, no hooks, no enum; (c)
everything else (`ReplanPolicy` class, 8-member `ReplanReason`,
`min_replan_interval` throttling, `watched_keys` diffing, `sensor_age`
gating, `plan_confidence` input, `state_snapshot`/`last_goal`/`executed`
bookkeeping, two new hook events) — host policy that duplicates state
the host already owns (it knows when it last planned, what it executed,
how old its sensor data is). Throttling is five lines at the call site:
`if now - last < interval: skip`.
The 90% design: the budget primitive + the validity predicate + a
worked example of a host loop composing them. The enum in particular is
speculative: 01 declares it "the stable vocabulary" for diagnostics, but
doc 06 explicitly wants *free-form, producer-owned* trigger strings
(§6(a)) — the supposed consumer of the vocabulary rejects it. Don't
standardize a vocabulary its consumer won't use.

---

## Constraint-violation check

| Constraint | Result |
|---|---|
| uv workflow | **PASS** — no new dependencies; stdlib only (`time`, `enum`, `dataclasses`). |
| Python ≥ 3.12 | **PASS** — `float \| None`, `str \| Enum`, `field(default_factory=...)`; nothing newer than 3.10 idioms. |
| 100% coverage, no `pragma: no cover` | **CONDITIONAL** — the design is implementable at 100%, but attacks 2, 13, 15 name branches the design leaves unspecified; the implementer would have to guess (violating the project's ask-don't-guess rule) to cover them. No `pragma` needed, but the spec gaps must be closed first. |
| ruff + mypy green | **PASS** (by inspection) — typed signatures throughout; `clock: Callable[[], float]` is fine. One nit: `detail: dict[str, Any]` on a frozen dataclass is fine for mypy. |
| No new required deps for core; `typesafe-sdk` stays `[jev]` extra | **PASS** — §5(a)/(d) correctly keep the policy on `float` inputs, never Jev types. |
| All new behavior opt-in / backward compatible; default behavior unchanged | **PASS with one caveat** — default paths verified unchanged (attack 19); the caveat is attack 17 (`on_search_failed` broadening for `time_budget` users with legacy hooks). Not a default-path change, but a hook-contract change that must be documented as such. |
| Public API via `goapauto/__init__.py` `__all__` | **PASS** — `ReplanPolicy`, `ReplanDecision`, `ReplanReason` named for `__all__` (plus `Planner.get_action`, a method, no export needed). |
| Soulscape touchpoints stable (`JevSensor.judge`, `shared_client`, `FakeTypeSafeClient`) | **PASS** — untouched; the design never references them. |

No hard FAILs. The verdict is driven by design defects, not constraint
violations.

## Interaction-consistency check (doc by doc)

### 02 — Sensor caching / staleness
- **Agreement:** threshold policy belongs to the budgets worker (02 says
  it, 01's `max_sensor_age` implements it); both default off; tick order
  "sense → freshness check → `should_replan` → plan" is compatible.
- **Contradiction 1:** 02 §5(a) says the budgets design "should consume
  `SensorReport.meta[*].age_seconds` and `staleness`" and offers shared
  helpers `max_age` / `oldest_staleness`. 01 §5(a) says the policy "takes
  a plain `float`, never `JevStats` — no dependency on Jev types." One
  side expects structured-report coupling, the other refuses it. *Someone
  must own the `SensorReport → float` translation (presumably the host),
  and both docs must say so.*
- **Contradiction 2:** 02 §5(a): "a replan triggered while a precondition
  key is STALE may deserve a tighter **node budget**". 01 has no
  per-call node budget — `max_iterations` is constructor-level only.
  The hook 02 proposes has nowhere to plug in.
- **Contradiction 3:** 02 assumes a "budget loop" with a "cheap pre-plan
  check" via `SensorManager.health()`. 01 builds no loop (non-goal 1)
  and `replan()` offers no pre-plan hook. 02's (a) is written against an
  architecture 01 explicitly refuses to build.

### 03 — Action interruption
- **Agreement:** interruption is boundary-only; 01 assumes
  `should_replan` is called between actions — consistent. Both agree the
  core performs no automatic interruption.
- **Contradiction 1:** 01 §5(b) asserts a contract — "an interrupted
  action must surface to the policy as `PLAN_INVALID` … and must bypass
  `min_replan_interval`". 03 provides no such signal. 03's interruption
  is honored at the *next action boundary* (03 §3, verified), so the
  interrupted action typically never started and its preconditions
  usually still hold → the policy's step 3 returns `PLAN_VALID` and
  execution *resumes* — the opposite of the asserted contract. The
  policy has no "was interrupted" input; the contract is unimplementable
  as specified.
- **Contradiction 2:** 03 introduces `begin_execution` / `PlanExecution`
  as the execution path, with `executed_actions` in the
  `PlanInterruptedError` payload. 01's policy tracks execution via
  `note_executed` calls the host must remember to make. If the host uses
  03's handle, `note_executed` is never called, the policy's `_executed`
  list is empty/stale, and `replan()`'s `continue_plan(executed_actions=
  ...)` strips the wrong prefix (or none). Neither doc wires the two
  together.
- **Contradiction 3:** 03 §5(a) asks "whether design 01 wants a
  synchronous predicate (boundary check) or an asynchronous watchdog
  thread" and says the "budget owner" wires up auto-interrupt on "tick
  budget exhausted". 01's non-goals 2 and 4 already answer: no execution
  budgets, no background threads — and 01 defines no tick budget at all
  (its `time_budget` is per-planning-call, not per-tick). The question is
  misaddressed: the only possible "budget owner" is the host, and 01
  should say so instead of leaving 03 waiting on an answer it already
  gave. Terminology collision: 03's "tick budget" ≠ 01's "time budget".

### 04 — Confidence gating
- **Agreement:** both place gating before the replan decision; both agree
  a low-confidence plan should replan rather than execute.
- **Contradiction 1:** 01 §5(c) says "(c)'s wrapper scores the fresh
  `PlanResult`" and `should_replan` takes `plan_confidence=`. Doc 04 has
  no plan scorer — its gating is per *answer* (sensor keys, strategy
  picks), and its §5(a) says it "runs strictly after the answer arrives",
  i.e. after a *judgment* call, not after planning. Nobody owns
  aggregating per-answer confidences into a plan confidence (min?
  product? weighted?). The input has no producer.
- **Contradiction 2:** 01 §5(c): "a budget-exhausted result …
  should feed confidence `0.0` on (c)'s side". In 01's own step ordering,
  `NO_PLAN` (step 2) short-circuits before `LOW_CONFIDENCE` (step 5), so a
  `plan=None` result can never reach the confidence check — the mapping
  is unreachable dead spec. It also collides with 04's "never coerce"
  rule (`None` → pass-through, never `0.0`) and 05's recommended
  treat-absent-as-1.0.
- **Open loop:** 04's open question 2 (flapping confidence scorer →
  replan storm) is exactly attack 8; neither doc treats it.

### 05 — Provider-independent judgment
- **Agreement:** the policy stays provider-agnostic (plain floats); 05
  keeps timeouts out of the protocol — compatible layering.
- **Contradiction:** 05 §6(a): "the replan-budget design treats a
  judgment call as a timed block like any other… `JudgmentResponse`
  carries `latency_ms` … so budget accounting can attribute time
  precisely." 01 does no budget accounting of sense/judgment latency at
  all — `time_budget` covers only the planner internals, and nothing in
  01 consumes `latency_ms`. If a tick spends 300 ms in `sense()` and
  50 ms planning, the 300 ms is outside every budget 01 enforces. 05
  assumes an accounting layer 01 doesn't build.

### 06 — Why-diagnostics
- **Agreement:** 06 renders unknown `stop_reason` strings verbatim —
  tolerant of 01's hook-based vocabulary. Both want the partial search
  graph available after exhaustion.
- **Contradiction 1:** 06 §6(a) specifies the budget worker emits
  `budget_exhausted` with `{budget_name, limit, consumed, unit, phase,
  goal, action}` and `replan_throttled` with `{reason, decision, trigger,
  last_replan_tick, cooldown_remaining_ms}`. 01 emits planner hooks
  `on_budget_exhausted(plan=None, stats=stats)` and
  `on_replan_skipped(decision=...)` — different names, different
  payloads, no `tick`/`seq`, no `limit`/`consumed`/`unit`,
  `cooldown_remaining_ms` not emitted (derivable from `elapsed`, but not
  emitted), `trigger` as free-form string not emitted (01 has the enum
  instead — which 06 explicitly doesn't want: "free-form string,
  producer-owned vocabulary"). No adapter is specified anywhere, and 06
  "never synthesizes these events itself" — so with both designs built
  as written, the inspector honestly reports "no budget events recorded"
  while budgets are firing. The integration is specified nowhere.
- **Contradiction 2:** 06's `budget_name` examples include
  `"replans_per_tick"`, `"execute_time_ms"`, `"search_nodes"` — budgets
  01 does not implement (01 has planning *time* only, no node budget, no
  execution budget, no per-tick replan count). 06 is consuming a budget
  subsystem larger than 01.
- **Agreement worth keeping:** 06's cross-worker contract tests use
  *synthetic* payloads — they will pass while the real producer/consumer
  mismatch above persists. Recommend at least one contract test built
  from 01's actual hook payloads, not synthetic dicts.

---

## What I'd force back into scope

1. An adoption primitive (`note_planned` / `adopt_plan`) — without it the
   policy cannot be used with any pre-existing plan (attack 13).
2. A replan circuit breaker (consecutive-trigger backoff or
   `max_replans_per_tick`) — without it the unthrottled triggers are a
   production livelock (attacks 7–8), and doc 06 already assumes the knob.
3. The 01↔06 event adapter (or an explicit decision that 01's hooks *are*
   the events, with field mapping) — without it the diagnostics layer is
   blind to budgets by 06's own "never synthesizes" rule.
4. Named ownership of the three orphaned translations: `SensorReport →
   sensor_age` (01↔02), per-answer confidences → `plan_confidence`
   (01↔04), judgment `latency_ms` → budget accounting (01↔05).

## What I'd cut

The `ReplanPolicy` as a stateful class (attack 23): ship the
`time_budget` primitive + a pure `next_action_valid`-style predicate +
a worked host-loop example. The 8-member enum, the sensor-age gate, the
confidence input, and the snapshot bookkeeping are host policy wearing a
library costume — and the enum's raison d'être is rejected by its
intended consumer (06).
