# RED-TEAM REVIEW — Design 03: Action Interruption

**Reviewer stance:** hostile. This doc is well-written and its core
mechanism (boundary-honored cooperative interruption) is sound — which is
exactly why the defects below matter: they are load-bearing, not cosmetic.
Three of them require spec changes, not clarifications.

**Verdict: NEEDS-REWORK**

Justification: (1) the why-diagnostics integration (doc 06) is broken as
specified — wrong hook name, disjoint field sets, disjoint reason
vocabularies, and a mid-action event model contradicting this doc's
between-actions axiom; (2) the boundary check order silently drops the
doc's own "allowed and common" interrupt pattern (interrupt from the final
action's `on_action_complete`) and leaves a spec'd branch unreachable and
therefore untestable under the 100%-coverage gate; (3)
`PlanInterruptedError(PlanExecutionError)` subclassing actively works
against the doc's own §5(e) goal of not conflating deliberate stops with
failures. Each needs a design decision and a spec edit before
implementation. The rest is fixable in the same pass.

## The three strongest attacks

1. **Doc 06 subscribes to a hook this doc never fires.** 06 binds to
   `on_action_interrupted` "if the hook exists" — 03 only defines
   `on_execution_interrupted`. The inspector will silently record zero
   interruptions forever. The field sets (`{action, step_index,
   plan_length, reason, detail, state_diff}` vs `{reason, source, state,
   executed_actions, next_action}`) and reason vocabularies
   (`precondition_broken | external_cancel | budget_exhausted |
   goal_changed` vs `manual | sensor | watchdog | budget | arbitration |
   confidence | cancellation | predicate`) have zero overlap, and 06's
   `action`/`state_diff`-from-`action_started` model assumes mid-action
   interruption, which 03 explicitly rejects.
2. **`PlanInterruptedError(PlanExecutionError)` is a backward-compat
   hazard the doc rationalizes as a feature.** "So existing `except
   PlanExecutionError` handlers observe it" — observing it *is* the bug.
   Every shared helper that catches `PlanExecutionError` to mean
   "execution failed" (log, alert, retry, count) will now swallow
   deliberate interruptions, which is precisely the failure-metrics
   poisoning §5(e) claims to prevent. The inheritance buys nothing for
   old code (`execute_plan` never raises the subclass) and taxes all new
   shared code.
3. **The documented check order cannot honor the documented common
   pattern.** §3 lists interrupt-from-`on_action_complete` as "allowed
   and common" — but there is no boundary check after the *last* action,
   so an interrupt raised there sets the flag, returns `True`, and is
   never honored: the run completes normally. `is_interrupted` semantics
   then contradict themselves, and the "interrupt after the last action"
   case the doc "defines for completeness" is unreachable except via a
   microsecond cross-thread race — untestable, which collides head-on
   with the 100%-coverage / no-`pragma` gate.

---

## Numbered attacks

### 1. The 06 contract is broken on four independent axes (hook name, fields, vocabulary, event model)

**Flaw.** Doc 06 §6(c) binds `{action, step_index, plan_length, reason,
detail, state_diff}`, subscribes to `on_action_interrupted` if present,
and expects reason values `precondition_broken`, `external_cancel`,
`budget_exhausted`, `goal_changed`. Doc 03 fires
`on_execution_interrupted` with `{reason, source, state,
executed_actions, next_action}` and conventional *source* values
`manual | sensor | watchdog | budget | arbitration | confidence |
cancellation | predicate`. Nothing lines up:
- The hook 06 subscribes to will never exist → the "if present" hedge
  becomes a permanent blind spot, and 06's open question 6 ("hook vs.
  exception is worker (c)'s call") was never actually answered *to* 06:
  03 chose "both, but renamed."
- 06's `reason` vocabulary reads like 03's `source` field wearing a
  false mustache — `budget_exhausted` vs `budget`, `external_cancel` vs
  `cancellation` — so even a rename adapter can't map them without a
  rosetta table neither doc owns.
- 06's `action` field and "`state_diff` between the state at
  `action_started` and at interruption" presuppose an *interrupted
  action*. 03's axiom is that actions are never interrupted —
  interruption happens *between* them. There is no `action_started`
  for the action that "was interrupted," because no such action exists.
  If interrupted before the first action, 06's recorder (which derives
  `step_index`/`plan_length` from the most recent `action_started`) has
  no index at all.

**Why it matters.** Concrete scenario: Soulscape trips a sensor
interrupt; the inspector's "why did the plan stop" view shows nothing —
not a wrong event, *no* event — because `on_action_interrupted not in
planner.hooks` is always true. The diagnostics differentiator silently
fails to differentiate.

**Fix.** 03 must own the event contract and 06 must conform to it (not
the reverse): pick ONE hook name, put `step_index` and `plan_length`
*in the hook kwargs and the error payload* (03 knows the index; 06
should not have to reconstruct it from `action_started` tea leaves),
and reconcile the vocabularies — see attack 12 for the
reason-vs-source split. 06's §6(c) then needs a rewrite against the
real event. This is cross-doc rework, which is why the verdict is not
"approve with changes."

### 2. `PlanInterruptedError(PlanExecutionError)` — the subclassing hazard

**Flaw.** §2.4: "Subclasses `PlanExecutionError` … so existing `except
PlanExecutionError` handlers observe it — but callers that care catch
`PlanInterruptedError` specifically." The first clause is stated as the
justification; it is the hazard. Any shared execution wrapper —
`try: run_anything() except PlanExecutionError: metrics.incr("failures");
retry_or_alert()` — now classifies a deliberate, healthy interruption
as a failure. §5(e) explicitly says conflating "stopped on purpose"
with "failed" would "poison failure metrics"; the exception hierarchy
does exactly that at the handling layer while the hook layer
de-conflates. The two layers disagree.

**Why it matters.** Concrete scenario: the Soulscape tick loop wraps
both `execute_plan` (old code) and `execution.run()` (new code) in one
`except PlanExecutionError` that pages on repeated failures. A watchdog
interrupting a stale plan every few ticks — correct behavior — now
pages as an execution-failure storm. The operator learns to ignore the
alert, and then misses a real one.

Note the blast radius is narrower than it looks, which makes the
inheritance even less defensible: existing `execute_plan` *never
raises* the subclass, so no old code path needs the catch to keep
working. The subclassing buys zero backward compatibility and sells
one new ambiguity.

**Fix.** Make `PlanInterruptedError` a sibling, not a child —
subclass `ValueError` directly (keeping the `ValueError` lineage the
rest of the library's errors share) or `Exception`. If the designer
insists on the inheritance, the doc must name the concrete old-code
path that requires it — there isn't one — and accept the metrics
poisoning as a documented cost. Sharp question for the designer: which
existing caller breaks if the inheritance is removed? If the answer is
"none," remove it.

### 3. The final-action interrupt is silently dropped; the "completeness" case is unreachable and untestable

**Flaw.** The §3 check order evaluates the interrupt condition *before
each action starts*. There is no check after the loop. Consequences:
- `interrupt()` called from the last action's `on_action_complete` —
  the doc's own "allowed and common" pattern (§3, "Interrupt from
  inside a hook or handler") — sets the flag, returns `True`, and is
  never consulted. The run completes normally and fires
  `on_execution_complete`. The caller was promised ("returns `True`"
  = "will be honored") something that did not happen.
- `is_interrupted` after such a run: the flag is set, but the doc says
  "`is_interrupted` stays `False` for a run that completed normally."
  Both cannot be true. The property's meaning is undefined here.
- Pre-run `interrupt()` on an *empty* plan: the loop body never
  executes, the flag is never consulted, `run()` returns the initial
  state as a normal completion. The doc's "Interrupt before the first
  action" section (which promises `on_execution_interrupted` fires
  once, `executed_actions == []`) does not cover this.
- The "interrupt requested after the last action but before
  `on_execution_complete` — vanishingly rare; defined for
  completeness" case (`next_action=None`, `remaining_actions=[]`) is
  unreachable through the documented check order except by winning a
  cross-thread race inside a microsecond window. It cannot be tested
  deterministically — and the test plan (§6) claims "every branch
  below is a test" under a 100%-coverage gate with no `pragma: no
  cover`. An untestable branch fails the gate by construction.

**Why it matters.** Concrete scenario: a Soulscape `on_action_complete`
hook detects the goal is already satisfied (or a sensor went stale)
during the *final* planned action and calls `execution.interrupt("goal
already satisfied", source="sensor")`. Expected: interruption recorded,
replan skipped. Actual: silent normal completion, `on_execution_complete`
fires, downstream logic treats the plan as fully executed — and the
why-diagnostics trail says "completed" for a run the operator
deliberately stopped. The loudness guarantee (§1: "must surface loudly,
never silent") is violated by the core's own recommended pattern.

**Fix.** Add two checks to §3: one *before* the loop (covers pre-run
interrupt and empty plans — fail fast, deterministic, trivially
testable) and one *after* the loop, before `on_execution_complete`
(covers the final-action hook case; this is what makes the
`next_action=None` case reachable and testable). Define:
post-loop flag set → fire `on_execution_interrupted`, raise
`PlanInterruptedError` with `remaining_actions=[]`,
`next_action=None`, instead of completing. Then `is_interrupted`
means "the run stopped because of an interrupt," with no
contradiction.

### 4. `CancelledError` "propagates unchanged" collides with the hook-supersede rule

**Flaw.** §3 promises: "the `CancelledError` **propagates unchanged** —
never swallowed, never converted." The failure-mode table promises:
"`on_execution_interrupted` hook raises → propagates … the
`PlanInterruptedError` is superseded by the hook's exception." On the
cancellation path there is no `PlanInterruptedError` — but the hook
still fires ("`on_execution_interrupted` fires on this path too"). If a
hook raises during cancellation cleanup, the hook's exception
*replaces* `CancelledError`. That is swallowing-and-converting by the
doc's own definition: `task.cancel()` then delivers the hook's error
instead of `CancelledError`, and `asyncio.wait_for` / `shield`
machinery upstream observes a task that failed rather than a task that
was cancelled. The two rules are jointly unsatisfiable.

**Why it matters.** Concrete scenario: `asyncio.wait_for(asyncio.shield(
task), timeout)` around `arun()`; the task is cancelled; an
`on_execution_interrupted` analytics hook raises (network blip writing
the trace). The caller sees the hook's `ConnectionError`, not
`CancelledError`/`TimeoutError` semantics — the structured-concurrency
contract the doc claims to preserve is broken by its own observability
hook.

**Fix.** On the cancellation path, hook exceptions must not supersede
`CancelledError`. Options the designer must pick: (a) do not fire
*user* hooks on the cancellation path at all (record the marking on
the handle only — the diagnostics trail survives via
`interrupt_source == "cancellation"`); or (b) fire hooks but chain,
never replace (`raise hook_error from cancelled_error` still changes
the delivered exception type — so (b) really means catch-and-log,
which contradicts fail-loud and needs explicit justification).
Sharp question: which is it — is `CancelledError` purity or hook
fail-loud the higher law on this path? The doc currently asserts
both.

A secondary gap on the same path: if an async *handler* catches and
suppresses the injected `CancelledError` and returns normally, `arun()`
continues the loop as if nothing happened — no marking, no flag (a
`task.cancel()` never sets the library's flag). Cancellation remains
cooperative with handlers, and a hostile handler defeats the marking
silently. The doc should state this plainly.

### 5. The thread-safety carve-out is under-specified and partially wrong

**Flaw.** "Implement with `threading.Event` (or equivalent)" cannot
deliver the promised "first `(reason, source)` wins … deterministic
under races between a watchdog thread and a same-thread hook."
`threading.Event.set()` is idempotent, but first-*payload*-wins needs
an atomic check-then-set on the `(reason, source)` pair — that is a
`Lock`-guarded flag, not an `Event`. An `Event` alone gives you
last-writer-wins on the payload under a race, which is exactly the
non-determinism the doc claims to exclude.

Worse, §2.3 advertises inspection "valid after run() starts;
`executed_actions` grows live" while §3 restricts cross-thread use to
`interrupt()` and `is_interrupted` ("everything else on the handle is
same-thread-only"). The doc's own §2.5 example blesses the watchdog
pattern: "From anywhere with a reference to the handle — another
thread, a hook, a watchdog, the Soulscape tick loop:
`execution.interrupt(...)`." A watchdog that interrupts and then reads
`execution.executed_actions` or `execution.state` to log *what* it
stopped — the obvious thing to do — iterates a list the worker thread
is concurrently appending to. `RuntimeError: list changed size during
iteration`, or torn reads, in the exact pattern the doc recommends.

**Why it matters.** The single blessed cross-thread interaction
produces a data race in its own example the moment anyone inspects the
result from the interrupting thread.

**Fix.** (a) Specify a `Lock`-guarded flag with atomic first-wins, not
a bare `Event`. (b) Make the inspection properties return immutable
snapshots (`tuple(...)` / defensive copies), so cross-thread *reads*
are safe even though cross-thread *control* is limited to
`interrupt()`. Then the carve-out can honestly be widened to
"interrupt + read-only inspection are thread-safe; everything else is
same-thread-only," which matches how the handle will actually be used.

### 6. Doc 01's "interrupted action" contract contradicts the atomicity axiom — and the host bridge is nobody's job

**Flaw.** 01 §5(b): "an interrupted action must surface to the policy
as `PLAN_INVALID` (the interrupted action's preconditions / effects
can no longer be assumed)." In 03 there is no such thing as an
interrupted *action*: interruption is strictly between actions, and
"the state at that point is always a fully-applied, consistent
`WorldState`" — executed actions' effects *can* be assumed; only the
never-started next action is suspect. 01's parenthetical describes
mid-action-abort semantics that 03's non-goal 3 explicitly rejects.
The two docs hold different mental models of what interruption *is*.

Further, 01 claims "the host calls `should_replan` after cancellation
and gets `PLAN_INVALID`." False in general. `should_replan(state,
goal, plan=err.remaining_actions, next_index=0)` returns
`PLAN_INVALID` only if the next action is *actually inapplicable* in
the partial state. A sensor-triggered interrupt where preconditions
still hold falls through to step 7 (`WORLD_CHANGED`, throttled by
`min_replan_interval`) or even step 8 (`PLAN_VALID` — keep executing
a plan you just deliberately stopped). A deliberate interrupt can
therefore be answered `THROTTLED`: the policy silently swallows the
control signal 03 promised would be honored deterministically.

**Why it matters.** Concrete scenario: watchdog interrupts on a stale
sensor reading (`source="sensor"`); the next action's preconditions
still hold; host dutifully calls
`policy.should_replan(state, goal, err.remaining_actions,
next_index=0)`; policy returns `(False, THROTTLED)` because the last
replan was 200ms ago. The interrupt — a deliberate control signal —
dies in the policy's throttle. Neither doc defines the host bridge
("catch `PlanInterruptedError` → force replan bypassing the
interval"); 03 §5(a) says "the budget owner wires it up," 01 §5(b)
says "the policy needs no change." Each points at the other.

**Fix.** 01 must stop saying "interrupted action" and restate the
contract in 03's terms: on `PlanInterruptedError`, the host passes
`plan=err.remaining_actions, next_index=0`, and — the missing piece —
the policy needs an explicit "interrupted, replan now, bypass the
interval" input (or the host must bypass `should_replan` entirely and
call `replan()` directly). Decide which; "the host figures it out" is
not a contract. Also correct 01's parenthetical: executed effects
stand, per the atomicity axiom.

### 7. "No preemptive sync-handler timeouts" — the accepted risk voids the budget story it leans on

**Flaw.** The failure table admits: "There is no bound on how late
that boundary is if a handler blocks indefinitely — that is the
caller's handler contract, not the library's." Honest — but §5(a)
then presents the budget wiring ("the Soulscape tick loop (or the
budget middleware) calls `execution.interrupt(reason="tick budget
exhausted", source="budget")`") as if it works generally. It does not
work for sync handlers at all: if `run()` is blocked inside a 30s
sync handler (a Soulscape action shelling out, a network call), the
watchdog's `interrupt()` waits at the flag, the tick loop is stuck
inside `run()` for 30s, every downstream tick is missed, and the
"blown tick budget auto-interrupts" story is void. The interruption
primitive provides *no liveness bound* for sync execution — and the
primary consumer is described as a tick loop.

**Why it matters.** For a *production-hardening* project, "documented
as accepted" is not a mitigation, it is a label. The doc's central
guarantee — "honored at the next action boundary after the request" —
has an unbounded "after" for exactly the handler type (`execution_handlers`
sync callables) the current API encourages.

**Fix (no new machinery required).** State the precondition plainly
in §5(a): the budget-interrupt wiring is effective *only* with
`arun()` + async handlers; any caller needing bounded interrupt
latency must not use sync handlers for slow actions. That turns an
implied guarantee into an explicit contract the Soulscape integrator
can actually plan around. Sharp question for the designer: is there
any Soulscape action whose handler is sync and slow? If yes, the
"accepted" risk is already realized.

### 8. Two seams for one signal: `interrupt_when` vs `interrupt()` + hooks

**Flaw.** The predicate seam (`interrupt_when(state,
next_action_name) -> str | None`) and the handle (`interrupt()` from
any hook/watchdog/thread) are two APIs for the same event. The
predicate's unique capabilities over
`on_action_complete`-hook-calls-`interrupt()`: (a) evaluation before
the *first* action without any hook having fired — but pre-run
`interrupt()` covers that via the flag (once attack 3's pre-loop
check exists); (b) seeing the *next* action's name — marginal, a hook
can track it. Meanwhile the predicate creates undecided semantics the
doc punts on: §7.3 asks whether a predicate firing sets
`source="predicate"`, but the §6 test plan already *assumes* it does
("decide: predicate-triggered interrupts get `source="predicate"`
automatically — document in implementation"). The test plan bakes in
an answer the design marks open. Also unspecified: flag set with
reason A *and* predicate returns reason B at the same boundary —
which reason wins? (Temporal first-wins says the flag; the doc never
says.)

**Why it matters.** Redundant seams double the test surface (predicate
× flag × thread × async matrix) and the doc already fails to specify
their composition. The 100%-coverage gate will force tests for
composition behavior the design hasn't decided.

**Fix.** Either cut `interrupt_when` (the handle + existing hooks
cover push and pull: a staleness layer registers
`on_action_complete`, checks its generation counter, calls
`interrupt()` — that *is* 02's pull wiring) or keep it and answer
§7.3 + the precedence rule in the spec, not the test plan. My
recommendation: cut it. It is the most cuttable seam in the doc (see
attack 14).

### 9. `err.state` aliasing — take the side: copy (answers §7.6)

**Flaw.** `err.state` *is* `execution.state` *is* the live partial
state — one mutable object with three owners (the error, the handle,
and whatever the caller passes to `continue_with` /
`continue_plan`). The failure table's advice — "callers needing
isolation copy it" — inverts the responsibility: the fail-loud
library hands out an alias and hopes the caller is careful.

**Why it matters.** Concrete scenario: caller catches
`PlanInterruptedError`, logs `err.state` through a diagnostics
pipeline that *annotates* the state in place (adds a
`_interrupted_at` key — plausible, 06 owns timestamps), then calls
`execution.continue_with(goal)`. The replan now starts from a state
containing a junk key that was never in the world. Silent corruption
of the replan input, from the doc's own recommended composition
path. Note `continue_plan`'s `_validate_and_convert` does *not* copy
a `WorldState` input (verified in source), so the alias flows
straight into planning.

**Fix.** Deep-copy at raise time. Cost: one `WorldState` copy per
interrupt — interrupts are rare by definition, and the state was
already deep-copied once at execution start (`execute_plan` does
`initial_state.copy(deep=True)`; the handle must do the same).
`execution.state` can stay live; `err.state` must be a snapshot.
This is the fail-loud-consistent choice, and it answers §7.6.

### 10. The handle state machine is implicit (terminal transitions, TOCTOU, `is_complete`)

**Flaws.**
- `is_complete` is undefined for interrupted and failed runs, yet the
  no-op rule ("`interrupt()` on a completed, failed, or
  already-interrupted execution is a no-op") depends on knowing what
  counts as terminal. After `run()` raises `PlanExecutionError`
  mid-plan with the interrupt flag *also* set (hook requested
  interrupt, then the next action's handler blew up), is
  `is_interrupted` true? The run did not stop *because of* the
  interrupt. The doc never defines the terminal transition.
- `interrupt_when` raising, or a hook raising, at a boundary leaves the
  handle in limbo: not complete, not interrupted, `run()` raised
  something else. May `interrupt()` be called afterward? May
  `continue_with`? Unspecified.
- Copy timing: does `begin_execution` deep-copy `initial_state`
  eagerly, or does `run()` copy at start (matching `execute_plan`)?
  If the caller mutates the state object between `begin_execution`
  and `run()`, which version executes? A TOCTOU the split entrypoint
  invents and the doc never addresses.
- `state` / `executed_actions` before `run()` starts: "valid after
  run() starts" — before that, property or error? Unspecified.

**Why it matters.** Each of these is a deterministic-test question
the 100%-coverage gate will force an answer to at implementation
time — i.e., decided under schedule pressure instead of in design.

**Fix.** Specify the state machine explicitly (states: `initialized →
running → completed | interrupted | failed`; terminal transitions and
what each property reports in each state), pin the deep-copy to
`run()`/`arun()` start (parity with `execute_plan`), and define
pre-run property behavior (raise `RuntimeError` or return
initial/empty — pick one).

### 11. `executed_actions` / `remaining_actions` normalization is load-bearing and half-specified

**Flaw.** §2.3 types them `list[str]`, and the examples show names —
but the plan may legally be a list of `Action` *objects*. The doc
never states that object steps are normalized to `action.name` when
recording. This matters because `continue_with` feeds
`self.executed_actions` into `continue_plan`'s prefix strip, which
compares `remaining_plan[0] == executed_name` — string equality
against freshly planned *names* (verified in source: `_find_plan`
returns `Plan = list[str]`). If an implementation recorded `Action`
objects (or the doc's silence lets one), the strip silently no-ops
and the replan replays already-executed actions — double-applying
effects. A silent behavioral fork hinging on an unspecified
normalization.

**Why it matters.** Concrete scenario: plan passed as
`[Action("open_door"), Action("go_through")]`; interrupt after the
first; `continue_with(goal)` replans from the partial state *without*
stripping `open_door` (comparison `str == Action` is `False`) →
`open_door` executes twice. If `open_door`'s effect is
non-idempotent, the world is now wrong and nothing raised.

**Fix.** Pin it in §2.3: "`executed_actions`, `remaining_actions`,
and `next_action` are always action-name strings; `Action`-object
steps are recorded by `action.name` at execution time." Add the
Action-object-steps interrupt test to §6 (it is currently only
listed under uninterrupted parity).

### 12. Reason taxonomy — take the side (answers §7.2)

**Flaw.** Free-form `reason` *and* free-form `source` with
"documented, not enforced" conventional values gives you the worst of
both: typo-prone grouping keys (`"bugdet"`) with no machine-checked
contract — which is exactly how 03 and 06 ended up with disjoint
vocabularies (attack 1) with nobody noticing until this review.

**My side.** Split the fields by their actual roles: `reason: str`
stays free-form and human (it is genuinely unbounded —
`"enemy_sighted"`, `"world generation moved 41 -> 42"`); `source`
becomes a closed `Literal["manual", "sensor", "watchdog", "budget",
"arbitration", "confidence", "cancellation", "predicate"]`. The
*source* taxonomy is the bounded set — it names *who* interrupted,
which is exactly what diagnostics grouping (06) needs to be stable.
Callers who need `"soul_mood"` put it in `reason`, where it belongs;
if a genuinely new source appears, it is a library-level addition,
which is correct because 06's rendering depends on it. This also
dissolves half of attack 1: 06's expected values were really *source*
values all along (`budget_exhausted` → `source="budget"`, etc.).

### 13. The 02 handoff references a component 03 does not define

**Flaw.** 02 §5(b): "the interruption checker calls
`sensor.last_report()` after each `update_state_detailed`." 03 has no
"interruption checker" role and no `update_state_detailed`. The
intended mapping (an `interrupt_when` predicate — or an
`on_action_complete` hook — closing over the sensor) is left to the
reader. Worse, 02 wants transitions "on keys the *running action*
depends on," but 03's predicate receives `(state, next_action_name)` —
the *next* action, at a boundary where the "running action" just
finished. The temporal framing does not match: 02 reasons about the
action that ran; 03's seam reasons about the action about to run.

**Why it matters.** The sensor-layer implementer and the interruption
implementer will build to different seams and discover the mismatch
at integration.

**Fix.** 03 should name the recommended 02 wiring explicitly (one
paragraph: "the staleness layer's pull wiring is an
`interrupt_when`/`on_action_complete` closure over
`last_report()`; the predicate's `next_action` argument names the
action whose preconditions are about to be evaluated, which is the
correct key-dependency scope — a just-completed action's keys are no
longer decision-relevant"). And 02 should stop saying "interruption
checker" and cite the actual seam.

### 14. Over-engineering: what to cut

- **`next_action` — cut as carried data.** It is `remaining_actions[0]
  if remaining_actions else None`, pure and total. Yet it is a
  constructor parameter of the error, a hook kwarg, and a diagnostics
  field — three surfaces that can drift (and, per attack 1, already
  did: 06 calls the same concept `action`). Keep it as a read-only
  convenience property if anyone wants it; stop carrying it as data.
- **`continue_with` — cut, or reduce to `**kwargs` forwarding.** It is
  a one-line wrapper (`planner.continue_plan(self.state, goal,
  self.executed_actions, ...)`) that duplicates a *subset* of
  `continue_plan`'s signature. The doc's own §2.5 example shows the
  one-liner it wraps. Signature drift is a matter of time: the day
  `continue_plan` gains a parameter, `continue_with` silently stops
  forwarding it. A handle whose job is *control* does not need a
  planning convenience method.
- **`interrupt_when` — cut** (see attack 8). Two seams, one signal;
  the handle covers pre-run, hooks cover per-boundary, threads cover
  async watchdogs. The predicate's marginal value does not pay for its
  composition semantics.
- **`source` free-form — constrain, don't cut** (attack 12:
  `Literal`).
- On Q1 (handle vs parameter): the handle is the right call *for the
  cross-thread interrupt case*, which a parameter on `execute_plan`
  cannot serve (the watchdog needs a reference to the in-flight run
  before it starts). So the separate entrypoint is justified — but
  only the *control* surface (`interrupt`, `is_interrupted`, `run`,
  `arun`, inspection). The bloat is not the handle; it is everything
  bolted onto it (`continue_with`, `interrupt_when`).

---

## Constraint-violation check

| Constraint | Verdict | Notes |
|---|---|---|
| uv workflow | **PASS** | Design-phase; test plan uses `uv run pytest`. Nothing contradicts. |
| Python >= 3.12 | **PASS** | `str \| None`, `list[str]`, `threading`/`asyncio` — all stdlib, no version hazards. |
| 100% coverage, no `pragma: no cover` | **FAIL as specified** | The "interrupt after last action before `on_execution_complete`" branch (attack 3) is unreachable except via a microsecond thread race — not deterministically testable. The CancelledError-during-hook-raise path (attack 4) is similarly hostile to deterministic coverage. Both become testable once attacks 3–4 are fixed (post-loop check; defined hook behavior on the cancel path). |
| ruff + mypy green | **PASS** | Nothing in the API is un-typeable; the attack-12 `Literal` for `source` *improves* typeability. |
| No new required dependencies | **PASS** | `threading`, `asyncio` stdlib only. |
| Opt-in / backward compatible; `execute_plan` default behavior UNCHANGED | **PASS with a flagged hazard** | `execute_plan`/`async_execute_plan` control flow, hook sets, and error types are untouched; the new hook event and `__all__` additions are additive. The flag is attack 2: `PlanInterruptedError(PlanExecutionError)` does not change old behavior but poisons *new* shared `except PlanExecutionError` handlers — a compat smell, not a default-behavior violation. |

## Interaction-consistency check

- **01 (runtime/replan budgets): CONTRADICTION + GAP.** (a) Who
  auto-interrupts on blown tick budget? 03 §5(a) says the budget
  owner wires it up; 01 defines *no* execute-phase auto-interrupt
  mechanism at all (its budgets are search-time deadlines and a
  between-actions `ReplanPolicy`) — and 06 §6(a) expects
  `budget_exhausted` events with `phase="execute"` and
  `budget_name="execute_time_ms"` that 01 never emits. Three docs,
  zero owners. (b) 01's "interrupted action … preconditions/effects
  can no longer be assumed" contradicts 03's atomicity axiom (attack
  6). (c) 01's "host calls `should_replan` after cancellation and gets
  `PLAN_INVALID`" is false in general — `THROTTLED`/`PLAN_VALID` can
  swallow a deliberate interrupt. *Agreement:* both place the
  interrupt decision in the budget layer, not the core, and both
  assume `should_replan` runs between actions.
- **02 (sensor caching/staleness): GAP, no direct contradiction.**
  Push wiring (`interrupt(source="sensor")` from a watcher) and pull
  wiring (predicate/hook closure over `last_report()`) are both
  mappable onto 03's seams, and both docs agree detection lives in
  02. But 02's "interruption checker" / `update_state_detailed`
  references a component 03 never defines, and 02's "keys the
  *running* action depends on" mismatches 03's `(state,
  next_action_name)` framing (attack 13).
- **04 (confidence gating): AGREEMENT.** Both docs say a gated
  mid-plan judgment never interrupts the in-flight plan; 03 offers
  the manual `interrupt(source="confidence")` seam and 04's
  perception-boundary-only stance is consistent with it. No
  contradiction found.
- **05 (provider-independent judgment): AGREEMENT.** Both say no
  touchpoint; 05 explicitly exempts judgment from cancellation
  semantics, 03 claims nothing over it. Clean.
- **06 (why-diagnostics): HARD CONTRADICTION** (attack 1). Wrong
  hook name subscribed (`on_action_interrupted` vs
  `on_execution_interrupted`), disjoint field sets, disjoint reason
  vocabularies, and a mid-action event model (`action`,
  `state_diff`-from-`action_started`) against 03's between-actions
  axiom. 06's "subscribes if present" hedge plus its open question 6
  mean the integration was never actually negotiated — 03 chose
  "both, renamed" without telling 06.

## Answers to the doc's open questions (reviewer's positions)

1. **Handle vs parameter:** handle justified (cross-thread needs a
   pre-run reference) — but strip the bolt-ons (attack 14).
2. **Reason taxonomy:** free-form `reason`, closed `Literal`
   `source` (attack 12).
3. **Predicate `source`:** moot if `interrupt_when` is cut; if kept,
   `"predicate"` must be in the closed `source` literal and stated in
   the spec, not the test plan.
4. **Budget wiring shape:** cannot be answered until 01 owns an
   execute-phase mechanism (interaction check, 01). Currently
   nobody's job.
5. **Timestamp clock:** defer to 06 — but note 06 currently cannot see
   the event at all (attack 1); settle the hook contract first.
6. **`err.state` ownership:** defensive deep copy at raise time
   (attack 9).
7. **`interrupt_all()`:** out of scope is correct; a planner driving
   concurrent executions can already hold the handles. Do not add.

---

*Review written 2026-09-21. Read-only review; no source, test, or
design file was modified. Codebase facts verified against
`src/goapauto/models/goap_planner.py` (`execute_plan`,
`async_execute_plan`, `continue_plan`, hook registry — 8 events
confirmed), `src/goapauto/models/worldstate.py`, and
`src/goapauto/__init__.py`.*
