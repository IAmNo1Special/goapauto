# Design 03 — Action Interruption

**Status:** implemented (Phase 2, 0.6.0). `PlanExecution`, `interrupt(reason, source=...)`, `on_execution_interrupted`.
**Scope:** cooperative interruption of in-flight plan execution, honored at
action boundaries only.
**Companion designs:** 01 (runtime/replan budgets), 02 (sensor caching/staleness),
04 (confidence gating), 05 (provider-independent judgment), 06 (why-diagnostics).
**Revision 2 (2026-09-21):** red-team review incorporated — see §8
(Adversarial review) for the disposition of all 14 attacks. Binding changes:
`PlanInterruptedError` is a `ValueError` sibling (not a `PlanExecutionError`
subclass); pre-loop and post-loop boundary checks added; `interrupt_when`
cut; `continue_with` cut; `next_action` cut as carried data (kept as a
read-only property); `source` is a closed `Literal`; hook name locked to
`on_execution_interrupted`, which the machinery **fires and then raises
through**.

---

## 1. Goal and non-goals

### Goal

Give callers a way to stop an in-flight `PlanExecution` run when the world
changes or a replan is triggered, **between actions** — never mid-action.
The interrupted run must surface loudly (never a silent `None`), must
preserve everything needed to replan (`partial state`, `executed_actions`,
`reason`), and must compose with the existing
`continue_plan(executed_actions=...)` replan path.

### The atomicity axiom (stated explicitly)

`Action.apply` / `Action.async_apply` — and any registered
`execution_handler` — are **atomic units**. There is no mid-action checkpoint,
no partial application, and no mechanism in the core that can observe or
interrupt the inside of an action. Interruption is therefore defined as:

> **An interrupt request is honored at the next action boundary, i.e. after
> the currently-running action (if any) completes and before the next action
> starts. The state at that point is always a fully-applied, consistent
> `WorldState`.**

### Non-goals (what we choose NOT to build, and why)

1. **Preemptive kill of a running sync `execution_handler`.** CPython cannot
   safely terminate a running thread; the library is explicitly not
   thread-safe ("one Planner per thread"). A thread-kill design would be
   unsafe, unportable, and would violate the atomicity axiom. Rejected.

2. **Per-action timeouts on synchronous handlers.** Enforcing a timeout on
   blocking sync code requires running it on a worker thread and abandoning
   the thread on expiry — same unsafety as (1), plus the library's
   not-thread-safe contract. Rejected for the core. (A caller that needs
   timeouts on *async* handlers can wrap their own handler in
   `asyncio.wait_for`; that policy belongs to the caller, not the library.)

3. **Mid-action checkpoints / partial application.** Would require actions to
   expose resumable state; `Action` has no such contract and adding one
   would redesign the action model. Rejected.

4. **Automatic world-change detection inside the core.** The core exposes the
   *seam* (the `interrupt()` handle) but owns no sensor polling loop, no
   staleness clock, no watcher thread. Detection is the sensor-caching/
   staleness design's (02) job. Rationale: separation of concerns; a polling
   loop in the core would impose threading and timing policy on every user.

5. **Pause/resume of the same execution handle.** Interruption is terminal
   for a handle. "Resume" is spelled as interrupt → `continue_plan` →
   `begin_execution` on the fresh plan. A suspend/resume primitive would
   duplicate the replan path for no gain. Rejected.

6. **Priority preemption (a more important plan evicting a running one).**
   Same mechanism as interruption — the arbiter interrupts the old run
   (`source="arbitration"`) and starts a new one. No separate primitive.

---

## 2. User-side API

### 2.1 New public symbols

New module `goapauto/models/execution.py`, re-exported through
`goapauto/__init__.py` (`__all__` extended):

- `PlanExecution` — the execution handle.
- `PlanInterruptedError(ValueError)` — raised when a run is interrupted.
  **Deliberately not a subclass of `PlanExecutionError`.** See §2.4 and
  attack 2 in §8.
- `InterruptSource` — `Literal["manual", "sensor", "watchdog", "budget",
  "arbitration", "confidence", "cancellation"]`.

New hook event: `"on_execution_interrupted"` (additive; existing eight hook
events unchanged). The machinery **fires this hook with its kwargs and then
raises** — fire-only-without-raise and raise-without-fire are both wrong.

### 2.2 Entrypoint

```python
class Planner:
    def begin_execution(
        self,
        initial_state: WorldState,
        plan: Plan | list[Action] | PlanResult,
        goal: Goal | None = None,
    ) -> PlanExecution: ...
```

- `plan` accepts the same shapes as `execute_plan` (names, `Action`s,
  `PlanResult`, tuple). Container/step *type* validation happens eagerly
  here (fail fast); action-name resolution stays lazy per step because
  providers are state-dependent.
- Steps are normalized to **action-name strings at `begin_execution` time**
  for all recording (`executed_actions`, `remaining_actions`); an
  `Action`-object step is recorded by `action.name`. Resolution of a name
  to its `Action` still happens lazily per step at execution time.
- The deep copy of `initial_state` happens at `run()`/`arun()` start, not
  at `begin_execution` — parity with `execute_plan`. A caller that mutates
  the state object between `begin_execution` and `run()` changes what
  executes (same TOCTOU contract `execute_plan` has between call and start).
- `execute_plan` / `async_execute_plan` are **unchanged** — default behavior
  (run to completion, raise on failure) is preserved bit-for-bit, and
  `execute_plan` **never** raises `PlanInterruptedError`.

### 2.3 The handle

```python
class PlanExecution:
    # --- control ---
    def interrupt(self, reason: str, *, source: InterruptSource = "manual") -> bool: ...
    @property
    def is_interrupted(self) -> bool: ...   # terminal state == "interrupted"

    # --- run ---
    def run(self) -> WorldState: ...
    async def arun(self) -> WorldState: ...

    # --- inspection (snapshots; safe to read cross-thread, see §3) ---
    @property
    def is_running(self) -> bool: ...
    @property
    def is_complete(self) -> bool: ...       # terminal state == "completed"
    @property
    def executed_actions(self) -> tuple[str, ...]: ...
    @property
    def remaining_actions(self) -> tuple[str, ...]: ...
    @property
    def state(self) -> WorldState: ...       # deep-copy snapshot
    @property
    def interrupt_reason(self) -> str | None: ...
    @property
    def interrupt_source(self) -> InterruptSource | None: ...
```

- `run()` executes synchronously to completion and returns the final
  `WorldState` — the same contract as `execute_plan`. If interrupted, it
  **raises** `PlanInterruptedError` (see §2.4). Precondition failures,
  handler exceptions, and hook errors raise exactly as `execute_plan` does
  today.
- `arun()` is the async twin: honors async handlers (`await
  handler(...)` / `await action.async_apply(...)`), same as
  `async_execute_plan`.
- `run()` / `arun()` are single-shot: a second call raises `RuntimeError`.
  (Prevents double-accounting of hooks and executed actions.)
- Inspection properties return **immutable snapshots** taken under the
  handle's lock: `executed_actions` / `remaining_actions` are fresh tuples,
  `state` is a fresh deep copy. This is what makes the documented
  watchdog pattern safe: a thread that calls `interrupt()` may then read
  `executed_actions` or `state` without racing the executing thread's
  appends.
- Pre-run property values (before `run()`/`arun()` starts):
  `is_running`/`is_complete`/`is_interrupted` are `False`;
  `executed_actions == ()`; `remaining_actions` is the full normalized plan;
  `state` is a deep copy of the passed `initial_state`;
  `interrupt_reason`/`interrupt_source` are `None`.
- There is **no** `continue_with` method. Replanning is spelled with the
  existing API (example in §2.5):
  `planner.continue_plan(err.state, goal, list(err.executed_actions))`.
  A one-line wrapper would duplicate a subset of `continue_plan`'s
  signature and drift the day `continue_plan` gains a parameter.

### 2.4 The interruption signal

```python
class PlanInterruptedError(ValueError):
    def __init__(
        self,
        message: str = ...,                 # deterministic default, see below
        *,
        state: WorldState,                  # defensive deep copy at raise time
        executed_actions: list[str],        # fresh list, name strings
        remaining_actions: list[str],       # fresh list, name strings
        step_index: int,                    # == len(executed_actions)
        plan_length: int,
        state_diff: dict[str, tuple[Any, Any]],  # initial.diff(interrupted): {key: (before, after)}
        reason: str,                        # required, non-empty, free-form
        source: InterruptSource,            # closed literal
    ) -> None: ...

    @property
    def next_action(self) -> str | None: ...  # remaining_actions[0] or None (convenience, not carried data)
```

- `PlanInterruptedError` subclasses **`ValueError` directly** — the same
  lineage as `PlanExecutionError` (which is also a `ValueError`), but as a
  **sibling, not a child**. Rationale: no existing code path needs the
  inheritance (`execute_plan` never raises it), while the inheritance would
  let existing `except PlanExecutionError` failure handlers — log, alert,
  retry, count — swallow deliberate interruptions as failures, poisoning
  exactly the failure metrics §5(e) exists to protect. The hook layer and
  the exception layer now agree: interruption is not failure.
- **Migration note for new shared wrappers:** code that wants to treat
  "execution did not reach the goal" uniformly should catch
  `(PlanExecutionError, PlanInterruptedError)` (or `ValueError`), with
  `PlanInterruptedError` caught first when the distinction matters.
  Existing code catching only `PlanExecutionError` is behaviorally
  unchanged, because no pre-existing path raises the new exception.
- Fail-loud, not silent: the interrupted call **raises**; there is no
  code path that returns `None` or a bare state on interrupt.
- `state` is a **defensive deep copy** taken at raise time — not the
  handle's live object. `execution.state` stays live; `err.state` is a
  snapshot. Callers can annotate, log, or hand `err.state` to
  `continue_plan` without aliasing the handle's internals (attack 9's
  in-place-annotation corruption scenario is closed by construction).
- `reason` is free-form and human (`"enemy_sighted"`,
  `"world generation moved 41 -> 42"` — genuinely unbounded).
  `source` is the closed `InterruptSource` literal: it names *who*
  interrupted, which is what diagnostics grouping needs to be stable.
  Callers needing `"soul_mood"` put it in `reason`; a genuinely new source
  is a library-level addition.
- `step_index` is the index of the next action that never started
  (`== len(executed_actions)`); `plan_length` is the total plan length.
  Both are carried so diagnostics (design 06) never has to reconstruct
  them from `action_started` tea leaves — in particular, an interrupt
  before the first action has no `action_started` at all.

### 2.5 Sync example

```python
from goapauto import Planner, PlanInterruptedError

planner = Planner(actions_list=[...])
plan = planner.generate_plan(state, goal).plan

execution = planner.begin_execution(state, plan, goal)

# From anywhere with a reference to the handle — another thread, a hook,
# a watchdog, the Soulscape tick loop:
execution.interrupt("enemy_sighted", source="sensor")

try:
    final = execution.run()
except PlanInterruptedError as err:
    print(err.reason)              # "enemy_sighted"
    print(err.executed_actions)    # ["move_to_cover", ...]
    print(err.next_action)         # action that never started (property)
    # Replan from the exact partial-state snapshot:
    new_plan = planner.continue_plan(err.state, goal, list(err.executed_actions))
```

### 2.6 Async example (pull wiring for sensor staleness, no predicate seam)

There is no `interrupt_when` predicate (cut — see §8, attack 8). The
staleness layer's pull wiring is an `on_action_complete` closure over its
own generation counter / `last_report()`:

```python
from goapauto import Planner, PlanInterruptedError

planner = Planner(actions_list=[...])
execution = planner.begin_execution(state, plan, goal)

def check_staleness(*, action, state) -> None:
    # Owned by the sensor-caching layer (design 02).
    if sensor.generation() != planned_generation:
        execution.interrupt(
            f"world generation moved {planned_generation} -> {sensor.generation()}",
            source="sensor",
        )

planner.register_hook("on_action_complete", check_staleness)

try:
    final = await execution.arun()
except PlanInterruptedError as err:
    new_plan = planner.continue_plan(err.state, goal, list(err.executed_actions))
```

Note the temporal framing (§5(b)): the check runs at a boundary *before*
the next action's preconditions are evaluated, so the correct
key-dependency scope is the *next* action — a just-completed action's keys
are no longer decision-relevant.

### 2.7 Hook

```python
planner.register_hook(
    "on_execution_interrupted",
    lambda *, reason, source, state, executed_actions, remaining_actions,
               step_index, plan_length, state_diff, interrupted_at: ...,
)
```

Fired **exactly once per interrupted run**, then the machinery raises —
*instead of* `on_execution_complete` (and never alongside
`on_execution_failed` — interruption is not failure). The action that never
started does **not** get `on_action_start`. The exact kwargs are binding;
see §9 (Producer contract). On the normal interrupt path, a hook that
raises propagates (consistent with all existing hooks), and the hook's
exception supersedes the `PlanInterruptedError` — same precedence rule as
today's `on_execution_failed` path. On the cancellation path the rule
differs (see §3): hook errors are captured and logged, `CancelledError`
still propagates unchanged.

## 3. Precise semantics, edge cases, defaults

### The handle state machine

States: `initialized → running → completed | interrupted | failed`.

- `initialized`: after `begin_execution`, before `run()`/`arun()`.
- `running`: inside `run()`/`arun()`.
- `completed`: `run()`/`arun()` returned normally. Terminal.
- `interrupted`: an interrupt was honored at a boundary (the flag path),
  or the run was cancelled (`source="cancellation"`). Terminal.
- `failed`: `run()`/`arun()` raised anything other than the interrupt
  signal (`PlanExecutionError`, `KeyError`, a hook exception, …). Terminal.

Property mapping: `is_running` ⟺ `running`; `is_complete` ⟺ `completed`;
`is_interrupted` ⟺ `interrupted`. All three read the state under the
handle's lock.

Terminal-transition rules (each deterministic and tested):

- If the interrupt flag was set but the run failed for another reason
  (e.g. a hook requested an interrupt, then the next action's handler blew
  up), the terminal state is `failed`, not `interrupted` — the run did
  not stop *because of* the interrupt. `is_interrupted` is `False`, but
  `interrupt_reason`/`interrupt_source` still report the first recorded
  request (diagnostics value, no ambiguity).
- If a hook raises inside `on_execution_interrupted` on the normal
  interrupt path, the terminal state is still `interrupted` — the run did
  stop because of the interrupt; only the reporting hook failed. The hook's
  exception is what propagates.
- On the cancellation path the terminal state is `interrupted` even
  though the delivered exception is `CancelledError`, not
  `PlanInterruptedError`.

### Boundary check order

**Pre-loop.** If the interrupt flag is set: take the interrupt path
(fire hook, raise). This covers interrupt-before-first-action and the
empty plan — deterministic, trivially testable. An empty plan with no
pending interrupt completes normally: `on_execution_complete` fires,
the state copy is returned.

**Per step *i* (0-based), before action *i* starts:**

1. Resolve the step to an `Action` (`KeyError` on unknown name — unchanged).
2. **Interrupt check.** If the interrupt flag is set: take the interrupt
   path. This check comes **before** the applicability check: an interrupt
   is a deliberate control signal, and any precondition fallout from the
   world change will surface on the subsequent replan (the partial state
   is preserved either way).
3. Applicability check → `on_action_failed` + `on_execution_failed` +
   `PlanExecutionError` (unchanged).
4. `on_action_start` → handler / `apply` → `on_action_complete` (unchanged).

**Post-loop.** After the last action's `on_action_complete` has fired:
if the interrupt flag is set, take the interrupt path with
`remaining_actions == []`, `step_index == plan_length` — *instead of*
firing `on_execution_complete`. Otherwise fire `on_execution_complete`
and return the final state.

The post-loop check is what makes interrupt-from-the-final-action's
`on_action_complete` (the "allowed and common" pattern) actually honored:
the flag, the `True` return, and the observation are consistent — no
silent drop, no unreachable branch.

### The interrupt path (flag honored)

1. Transition to `interrupted` (under the lock).
2. Snapshot: `state_copy = working_state.copy(deep=True)`;
   `state_diff = initial_snapshot.diff(state_copy)` → `{key: (before,
   after)}`, where `initial_snapshot` is the deep copy taken at
   `run()`/`arun()` start.
3. Fire `on_execution_interrupted` with the exact kwargs in §9. The
   `state` kwarg and `err.state` are the **same** snapshot object
   (one copy, consistent).
4. Raise `PlanInterruptedError` carrying the snapshot.

### Interrupt before the first action

Zero actions executed. `executed_actions == []`, `remaining_actions` is
the full plan, `step_index == 0`, `state` is a copy of the initial state,
`state_diff == {}`. `on_execution_complete` never fires;
`on_execution_interrupted` fires once.

### Interrupt after completion

`interrupt()` on a completed, failed, or already-interrupted execution is
a **no-op returning `False`**. `is_interrupted` stays `False` for a run
that completed normally. Rationale: completion is terminal; silently
flipping a finished handle's state would corrupt `why`-diagnostics.

### Double interrupt

First call wins: it records `(reason, source)` and returns `True`. Later
calls return `False` and change nothing. `interrupt_reason` always
reflects the first request — deterministic under races between a watchdog
thread and a same-thread hook.

### `interrupt()` argument validation

Validation runs **before** the terminal/no-op check (fail-loud on API
misuse is the higher law):

- `reason` must be a non-empty, non-whitespace string; anything else
  raises `ValueError` immediately (at the call site, not at the boundary).
- `source` must be a member of the `InterruptSource` literal; anything
  else raises `ValueError` immediately. Conventional mapping:
  `"manual"` (default), `"sensor"`, `"watchdog"`, `"budget"`,
  `"arbitration"`, `"confidence"`; `"cancellation"` is set internally on
  the cancellation path (a caller may also pass it explicitly to declare
  provenance).

### Long-running sync `execution_handlers`

**Cannot be preempted — stated explicitly.** If `interrupt()` is called
while a sync handler is running, the flag waits; the handler runs to
completion; the interrupt is honored at the next boundary. The guarantee
is exactly: *"interruption is honored at the next action boundary after
the request."* There is no bound on how late that boundary is if a
handler blocks indefinitely — that is the caller's handler contract, not
the library's. Consequence for the budget story: see §5(a).

### `interrupt()` from inside a hook or handler

Allowed and common (e.g. `on_action_complete` notices a dirty sensor flag).
It does **not** abort the action that just completed (it already completed)
and does not retroactively un-fire hooks. It takes effect at the next
boundary per the check order above — including the post-loop check, so an
interrupt from the *last* action's `on_action_complete` is honored.

### Thread-safety of the handle — the exact guarantee

The library remains not-thread-safe overall (one `Planner` per thread).
For `PlanExecution`, the supported cross-thread interactions are
**`interrupt()` and the read-only inspection properties**
(`is_running`, `is_complete`, `is_interrupted`, `executed_actions`,
`remaining_actions`, `state`, `interrupt_reason`, `interrupt_source`).

What is guaranteed, precisely:

- **Flag set is atomic with first-wins semantics.** `interrupt()` takes
  the handle's `threading.Lock`, validates args, checks terminal state,
  and — only if no request is recorded yet — records `(reason, source)`
  and sets the flag, returning `True`. Two racing `interrupt()` calls
  cannot both return `True` and cannot tear the payload. (A bare
  `threading.Event` cannot deliver this; the payload needs the lock.)
- **Honoring happens on the executing thread, at the next boundary.**
  The lock is never held across a boundary check or an action. No
  cross-thread machinery observes or mutates the inside of an action.
- **Inspection reads are snapshots under the lock.** `executed_actions`
  / `remaining_actions` are built as fresh tuples while holding the lock,
  so a watchdog thread iterating them cannot see a torn list;
  `state` is a fresh deep copy. The state flags are read under the lock.

What is **not** guaranteed: `run()`/`arun()` must be called on one
thread only; calling them concurrently, or calling `run()` and `arun()`
on the same handle, is undefined behavior. No memory-visibility promises
beyond the lock above.

### Async cancellation (`asyncio.CancelledError`)

- If the task running `arun()` is cancelled (e.g. `task.cancel()`), the
  `CancelledError` **propagates unchanged** — never swallowed, never
  converted. Swallowing it would break asyncio cancellation semantics.
- Before re-raising, the execution marks itself interrupted with
  `reason="task cancelled"`, `source="cancellation"` (first-wins still
  applies: if a manual interrupt already recorded a reason, that reason
  stands). The snapshot and `state_diff` are computed as on the flag
  path; `on_execution_interrupted` **fires** on this path too — it is
  still an interruption, observably, and the diagnostics subscriber
  (design 06) must see it.
- **Cancellation purity is the higher law on this path.** If a hook
  raises while handling the cancellation, the hook's exception must not
  supersede `CancelledError` (doing so would break
  `asyncio.wait_for`/`shield` structured-concurrency contracts upstream).
  The implementation captures the hook exception, reports it through the
  planner's logger, and re-raises the `CancelledError` unchanged. This is
  the one path where hook fail-loud yields to a stronger invariant, and
  it is stated here rather than left to inference.
- `interrupt()` during `arun()` from another task sets the flag; it is
  honored at the next boundary. It does **not** inject cancellation into
  a currently-awaited handler — cancelling someone else's in-flight
  coroutine is hostile and unpredictable; the boundary guarantee is the
  contract.
- **Stated limitation:** if an async *handler* catches the injected
  `CancelledError` and returns normally, `arun()` continues the loop as
  if nothing happened — no marking, no hook (`task.cancel()` never sets
  the library's flag). Cancellation remains cooperative with handlers,
  and a handler that suppresses cancellation defeats the marking
  silently. Documented, not fixed: the library will not second-guess a
  handler's explicit choice.
- `arun()` called outside a running event loop raises `RuntimeError`
  (standard asyncio behavior surfaces naturally; no special-casing).

---

## 4. Failure-mode table

| Failure mode | Handling |
|---|---|
| Interrupt requested while a sync handler runs | Flag waits; honored at next action boundary. No preemption (see §1, non-goal 1). |
| Interrupt requested while an async handler is awaited | Same: honored at next boundary; no cancellation injected into the handler. |
| `task.cancel()` during `arun()` | `CancelledError` propagates unchanged; execution marked interrupted (`source="cancellation"`); `on_execution_interrupted` fires; a hook raising on this path is captured and logged, never supersedes `CancelledError`; partial-state snapshot retained on the error (which is not raised — the `CancelledError` is). |
| Async handler swallows the injected `CancelledError` | `arun()` continues as if uninterrupted; no marking, no hook. Documented cooperative-cancellation limitation. |
| Interrupt after normal completion / failure / prior interrupt | No-op; `interrupt()` returns `False`; handle state unchanged. |
| Double interrupt (incl. thread race) | First `(reason, source)` wins atomically under the lock; subsequent calls return `False`. |
| `interrupt()` with empty/`None`/whitespace reason | `ValueError` immediately at the call site (before the terminal check). |
| `interrupt()` with unknown `source` | `ValueError` immediately at the call site. |
| Interrupt pending *and* next action's preconditions now false | Interrupt wins (checked first); `PlanInterruptedError` raised; precondition fallout surfaces on replan from the preserved partial state. |
| Interrupt pending before the loop (incl. empty plan) | Pre-loop check: `on_execution_interrupted` fires once, `PlanInterruptedError` raised with `executed_actions == []`, `step_index == 0`. |
| Interrupt pending after the last action | Post-loop check: `on_execution_interrupted` fires, `PlanInterruptedError` raised with `remaining_actions == []`, `next_action is None`; `on_execution_complete` does *not* fire. |
| `on_execution_interrupted` hook raises (normal path) | Propagates (consistent with all existing hooks); the `PlanInterruptedError` is superseded by the hook's exception — same precedence rule as today's `on_execution_failed` path. Terminal state remains `interrupted`. |
| `run()` / `arun()` called twice | `RuntimeError` ("execution already run"). Handles are single-shot. |
| `arun()` with no running event loop | Natural `RuntimeError` from asyncio; no special-casing. |
| Unknown action name in plan | `KeyError` at that step's resolution, unchanged from `execute_plan`. |
| Precondition failure mid-run (no interrupt) | Unchanged: `on_action_failed` + `on_execution_failed`, `PlanExecutionError`. |
| Flag set, then a handler raises before the next boundary | Terminal state is `failed`; `is_interrupted` is `False`; `interrupt_reason` still reports the recorded request. |
| Async handler passed to `run()` | `TypeError`, unchanged from `execute_plan`. |
| `begin_execution` given a bad plan container/step types | `TypeError`/`ValueError` eagerly at `begin_execution` (fail fast), matching `execute_plan`'s validation rules. |
| Caller mutates `err.state` after catching | Safe by construction: `err.state` is a defensive deep copy, not the handle's live object. `execution.state` is unaffected. |
| Plan steps passed as `Action` objects | Recorded by `action.name` everywhere; `continue_plan`'s prefix strip works on the name strings. |

---

## 5. Interaction notes with the other five differentiators

**(a) Runtime / replan budgets (design 01).**
The core defines **no** budget and performs **no** automatic interruption.
A blown tick budget auto-interrupts only if the budget owner wires it up:
the Soulscape tick loop (or the budget middleware) calls
`execution.interrupt(reason="tick budget exhausted", source="budget")`.
**Who decides: the budget layer, not the core.** The core's only
obligation is the prompt, deterministic boundary check (§3).

**Explicit precondition (no implied guarantee):** the budget-interrupt
wiring provides a *bounded* response only with `arun()` + async handlers.
With sync handlers the interrupt latency is unbounded (a blocked sync
handler delays every boundary indefinitely — §3), so a tick loop that
needs a hard bound must not run slow actions through sync handlers. This
turns the failure table's "accepted risk" into a contract the Soulscape
integrator can plan around.

**Host bridge contract (03's side, binding):** on catching
`PlanInterruptedError`, the host must treat it as an *explicit replan
command* and replan immediately — it must **not** route the decision
through a throttled `ReplanPolicy.should_replan`, which could answer
`THROTTLED` (or even `PLAN_VALID`) and silently swallow the deliberate
control signal. If the host uses the policy, it bypasses the interval
for this input. Further, there is no such thing as an "interrupted
action": executed actions' effects **stand** per the atomicity axiom;
only never-started actions are suspect. (Design 01's §5(b) parenthetical
describing an interrupted action's preconditions/effects as un-assumable
is 01's wording to fix — flagged for the 01 reviser.)

Open coordination point: whether design 01 wants an asynchronous watchdog
thread calling `interrupt()` — the supported pattern; see §7 (residual
question, owned by 01).

**(b) Sensor caching / staleness (design 02).**
World-change detection as an interrupt trigger: the signal originates
**outside** the core. Two supported wirings, both first-class:
1. *Push*: a sensor watcher (thread/coroutine in the staleness layer)
   calls `execution.interrupt(reason=..., source="sensor")` when cached
   readings go stale or a generation counter moves.
2. *Pull*: an `on_action_complete` closure over the generation counter /
   dirty flag captured at plan time calls `execution.interrupt(...)`
   when the world moved (example in §2.6).
The core never polls sensors and owns no staleness clock (non-goal 4).
Design 02 owns the definition of "the world changed"; this design owns
the deterministic boundary at which that signal takes effect.

**Recommended 02 wiring (named explicitly):** the staleness layer's pull
wiring is the `on_action_complete` closure over `sensor.last_report()`
(or the generation counter). The boundary check runs *before* the next
action's precondition evaluation, so the key-dependency scope is the
*next* action's keys — a just-completed action's keys are no longer
decision-relevant. (Design 02's "interruption checker" phrasing is 02's
to align to this seam — flagged for the 02 reviser.)

**(c) Confidence gating (design 04).**
A gated (low-confidence) judgment **mid-plan does not interrupt the
in-flight plan**. The judgment already fired and its action is atomic —
interruption cannot un-fire it. Gating is a *planning-time* concern; it
affects the **next** replan (fresh judgments, possibly a different plan),
never the current run. The only sanctioned touchpoint in the other
direction: a confidence layer that decides the current plan is untrustworthy
may call `interrupt(reason=..., source="confidence")` to force an early
replan — but already-executed actions stand, and the partial state is what
the replan starts from.

**(d) Provider-independent judgment interface (design 05).**
**No touchpoint.** Interruption is orthogonal to how judgments are produced
or which provider backs them. (If design 05 introduces a judgment record
type, the why-diagnostics event in (e) may reference its ID — that's 05/06
coordination, not this design's.)

**(e) Why-diagnostics (design 06).**
Every interruption must be traceable. The canonical record is the
`PlanInterruptedError` payload plus the `on_execution_interrupted` hook
kwargs — both carry the same fields, defined **exactly once** in §9
(Producer contract), which the 06 reviser builds against verbatim. This
design guarantees the event exists, fires exactly once per interrupted
run (fire-then-raise, including the cancellation path), and carries
enough to reconstruct *what stopped, where, and why*. Note the deliberate
choice: interruption is **not** routed through `on_execution_failed` —
conflating "stopped on purpose" with "failed" would poison failure metrics.

---

## 6. Test plan sketch (100% coverage gate holds)

New tests live in `tests/goapauto/models/test_execution.py` (+ async cases
under `asyncio_mode = auto`; no `pragma: no cover` in source — every branch
below is a test).

**Handle lifecycle**
- `begin_execution` returns a `PlanExecution`; pre-run property values
  (`is_running`/`is_complete`/`is_interrupted` False, `executed_actions`
  empty, `remaining_actions` full plan, `state` a copy of the initial
  state); `run()` returns final `WorldState` for an uninterrupted plan
  (parity with `execute_plan`, incl. `PlanResult` input, tuple input,
  `Action`-object steps, `goal=` provider context).
- `run()` twice → `RuntimeError`; `arun()` twice → `RuntimeError`.
- `begin_execution` with invalid plan container/step types → `TypeError`/
  `ValueError` eagerly (before `run()`).
- Caller mutating `initial_state` between `begin_execution` and `run()` →
  the mutation flows in (documents the TOCTOU parity with `execute_plan`).

**Interruption semantics**
- Interrupt before first action (flag set pre-`run()`): `executed_actions
  == []`, `remaining_actions == full plan`, `step_index == 0`,
  `state_diff == {}`, `on_execution_interrupted` fired once with the exact
  kwarg set (§9), `on_execution_complete` *not* fired, no
  `on_action_start` for any action.
- Interrupt between actions N and N+1 (via `on_action_complete` hook calling
  `execution.interrupt(...)`): partial state equals hand-computed expected
  state; `executed_actions` correct; `err.state` consistent.
- Interrupt from the **last** action's `on_action_complete`: honored at
  the post-loop check — `remaining_actions == []`, `step_index ==
  plan_length`, `err.next_action is None`, `on_execution_complete` *not*
  fired. (This is the regression test for the old silent drop.)
- Empty plan + pre-run interrupt → interrupted path; empty plan without
  interrupt → completes, `on_execution_complete` fires.
- Interrupt after completion → returns `False`, no state change,
  `is_interrupted` is `False`.
- Double interrupt → first `(reason, source)` wins; second returns `False`.
- `interrupt("")` / `interrupt(None)` / `interrupt("   ")` → `ValueError`;
  `interrupt("x", source="bogus")` → `ValueError`. Validation precedes the
  terminal no-op check (interrupt with bad args on a completed handle →
  `ValueError`).
- Interrupt pending + next action inapplicable → `PlanInterruptedError`
  wins over `PlanExecutionError`; replan from `err.state` then surfaces the
  precondition issue (integration assertion).
- `PlanInterruptedError` is a `ValueError` **and not** a
  `PlanExecutionError` (sibling assertion); carries all payload fields;
  `next_action` property derives from `remaining_actions`.
- `state_diff` content: interrupt after a state-changing action →
  `{key: (before, after)}` matches `initial.diff(interrupted)`.
- `err.state` is a defensive copy: `err.state is not execution.state`;
  mutating `err.state` leaves `execution.state` unchanged.
- Flag set, then handler raises before the next boundary → terminal
  state `failed`; `is_interrupted` False; `interrupt_reason` still
  reports the recorded request.

**Sync non-preemption (explicit)**
- A handler that sets the interrupt flag on itself (or sleeps then a
  watchdog thread interrupts mid-handler): assert the handler ran to
  completion *before* the boundary check fired — i.e. interruption is
  observed only at the next boundary, never inside the handler.

**Async**
- `arun()` completes a plan with async handlers (parity with
  `async_execute_plan`, incl. async-handler-in-`run()` → `TypeError`).
- `interrupt()` from another task between awaits → honored at next boundary.
- `task.cancel()` during `arun()` → `CancelledError` propagates (use
  `pytest.raises(asyncio.CancelledError)`); terminal state `interrupted`;
  `interrupt_source == "cancellation"`; `on_execution_interrupted` fired.
- Hook raising on the cancellation path → `CancelledError` **still**
  propagates (hook error captured and logged, not superseding).
- Handler that swallows the injected `CancelledError` and returns normally
  → `arun()` continues; no marking, no hook (documents the stated
  limitation).
- Interrupt flag set *during* an awaited handler → handler completes
  first (no injection), then boundary honors it.

**Hooks**
- `on_execution_interrupted` registered via `register_hook` (unknown-event
  `ValueError` still raised for typos — existing behavior).
- Hook raising on the normal path → hook exception propagates
  (`PlanInterruptedError` superseded); terminal state stays `interrupted`.
- Exact hook ordering test: `on_action_start`/`on_action_complete` × N,
  then `on_execution_interrupted`, and nothing after.
- Exact kwarg-set test: the fired kwargs are exactly the §9 set.

**Replan composition**
- After catching `PlanInterruptedError`,
  `planner.continue_plan(err.state, goal, list(err.executed_actions))`
  returns a valid `PlanResult`; executing the continued plan from
  `err.state` reaches the goal (end-to-end integration test).
- Plan passed as `Action` objects, interrupt mid-plan →
  `executed_actions`/`remaining_actions` are name strings and the
  continued plan does not replay executed actions (prefix strip works).

**Threading**
- Watchdog thread calling `interrupt()` while `run()` executes on the main
  thread: barrier-synchronized race → exactly one `True`, first
  `(reason, source)` wins deterministically; no deadlock, no torn state.
- Cross-thread reads: interrupting thread iterates
  `execution.executed_actions` and reads `execution.state` while the worker
  runs — no `RuntimeError`, snapshots are self-consistent.

**Backward compatibility**
- Full existing suite unchanged and green: `execute_plan` /
  `async_execute_plan` behavior, hook sets, and error types are untouched
  (the new code path must not alter their control flow); `execute_plan`
  never raises `PlanInterruptedError`.

---

## 7. Open questions — resolved and residual

Resolved in this revision (no longer open):

1. **Handle vs parameter** — handle kept; justified by the cross-thread
   interrupt case (a parameter on `execute_plan` cannot give a watchdog a
   reference to the in-flight run before it starts). Bolt-ons
   (`continue_with`, `interrupt_when`) cut.
2. **Reason taxonomy** — free-form `reason: str`, closed
   `source: InterruptSource` literal. The source set is what diagnostics
   grouping keys on; genuinely new sources are library-level additions.
3. **Predicate-triggered `source`** — moot: `interrupt_when` is cut.
5. **Timestamp clock for `interrupted_at`** — wall clock (`time.time()`),
   human-readable for diagnostics. If 06 needs monotonic durations, it
   records its own.
6. **Ownership of `err.state`** — defensive deep copy at raise time;
   `execution.state` stays live. One copy per interrupt; interrupts are
   rare by definition.
7. **`interrupt_all()`** — out of scope, stays out. A planner driving
   concurrent executions already holds the handles.

Residual (owned elsewhere):

4. **Budget wiring shape** — deferred to design 01: 01 must own an
   execute-phase mechanism before the primary wiring pattern (watchdog
   thread vs tick-loop-driven `interrupt()`) can be picked. This design
   supports both.

---

## 8. Adversarial review — disposition of all 14 attacks

**Attack 1 — the 06 contract is broken (hook name, fields, vocabulary,
event model). → RESOLVED (03's side).** Coordinator ruling B locks the
hook name to `on_execution_interrupted` and mandates fire-then-raise; the
diagnostics doc subscribes to that exact name. The producer contract (§9)
now defines the kwargs exactly once: `reason, source, state,
executed_actions, remaining_actions, step_index, plan_length,
state_diff, interrupted_at`. `step_index`/`plan_length` are carried so 06
never reconstructs them from `action_started` (impossible for
before-first-action interrupts). The reason/source split (attack 12)
dissolves the vocabulary collision: 06's old values were source values
(`budget_exhausted` → `source="budget"`). The 06-side rewrite against
this contract is the 06 reviser's job — flagged, not silently dropped.

**Attack 2 — `PlanInterruptedError(PlanExecutionError)` subclassing hazard.
→ RESOLVED.** Now a sibling: `class PlanInterruptedError(ValueError)`,
directly under `ValueError` like `PlanExecutionError`. The reviewer's
sharp question is answered in §2.4: no existing caller breaks, because
`execute_plan` never raises the new exception — the inheritance bought
zero backward compatibility and sold one ambiguity. Migration guidance
for new shared wrappers is specified. The unreachable-branch concern is
eliminated: there is no subclass branch to be unreachable.

**Attack 3 — final-action interrupt silently dropped; "completeness" case
unreachable/untestable. → RESOLVED.** §3 now has a pre-loop check (covers
pre-run interrupt and empty plans — deterministic, trivially testable)
and a post-loop check before `on_execution_complete` (covers the
final-action `on_action_complete` pattern). The flag/`True`/observation
trio is consistent, `is_interrupted` means "stopped because of an
interrupt" with no contradiction, and the old "vanishingly rare" case is
now the ordinary post-loop path with a regression test.

**Attack 4 — `CancelledError` purity vs hook-supersede rule.
→ RESOLVED.** Cancellation purity is declared the higher law on the
cancellation path: the hook still fires (coordinator ruling), but a hook
raising there is captured and logged through the planner's logger — it
never supersedes `CancelledError`, which propagates unchanged. On the
normal interrupt path the existing hook fail-loud rule stands
(supersede, consistent with `on_execution_failed`). The secondary gap is
stated plainly in §3: a handler that swallows the injected
`CancelledError` defeats the marking silently — documented limitation,
with a test pinning the behavior.

**Attack 5 — thread-safety carve-out under-specified (Event vs Lock;
cross-thread inspection race). → RESOLVED.** §3 now specifies a
`threading.Lock`-guarded flag with atomic first-wins (an `Event` alone
cannot deliver payload atomicity), and inspection properties return
immutable snapshots (tuples, deep-copied state) taken under the lock.
The carve-out is honestly widened to "interrupt + read-only inspection
are thread-safe; `run()`/`arun()` are single-thread-only" — matching the
doc's own watchdog example, which previously raced.

**Attack 6 — 01's "interrupted action" contradicts the atomicity axiom;
host bridge is nobody's job. → RESOLVED (03's side); 01-side wording
DEFERRED to the 01 reviser.** §5(a) now states the binding host contract:
on `PlanInterruptedError`, replan immediately and bypass any replan
throttle — a deliberate interrupt is an explicit replan command, never
`THROTTLED`/`PLAN_VALID`. It also restates that executed effects stand
and no "interrupted action" exists. Fixing 01's §5(b) parenthetical is
out of this file's write scope — flagged for the 01 reviser, not dropped.

**Attack 7 — no liveness bound voids the budget story. → RESOLVED.**
§5(a) now states the precondition explicitly: bounded interrupt latency
requires `arun()` + async handlers; sync handlers make the latency
unbounded. The integrator plans around a contract, not an implication.

**Attack 8 — two seams for one signal (`interrupt_when` vs handle).
→ RESOLVED.** `interrupt_when` is cut per the coordinator's cut list.
The handle covers pre-run (flag), per-boundary pull (hook closure, §2.6),
and async watchdogs (threads). Q3 is moot with it.

**Attack 9 — `err.state` aliasing. → RESOLVED.** Defensive deep copy at
raise time (§2.4, §3); `execution.state` stays live. Answers Q6. The
reviewer's in-place-annotation corruption scenario is closed by
construction, with a test asserting `err.state is not execution.state`.

**Attack 10 — implicit state machine. → RESOLVED.** §3 specifies the
machine (`initialized → running → completed | interrupted | failed`),
the property mapping, and the terminal-transition rules (flag-set-then-
failed → `failed` with `is_interrupted` False but reason inspectable;
hook-raises-on-interrupt-path → still `interrupted`;
cancellation → `interrupted` while delivering `CancelledError`). Deep
copy pinned to `run()`/`arun()` start (parity with `execute_plan`);
the between-`begin_execution`-and-`run()` TOCTOU is documented, not
hidden. Pre-run property values are pinned.

**Attack 11 — action-name normalization half-specified. → RESOLVED.**
§2.2 pins it: steps normalize to name strings at `begin_execution`;
`executed_actions`/`remaining_actions`/`next_action` are always name
strings. The double-apply fork (recorded `Action` objects silently
no-op'ing `continue_plan`'s prefix strip) is closed, with an
`Action`-object-steps test in §6.

**Attack 12 — reason taxonomy. → RESOLVED** per the coordinator (take
the reviewer's side): free-form `reason`, closed `InterruptSource`
literal for `source`. Answers Q2.

**Attack 13 — 02 handoff references an undefined component.
→ RESOLVED (03's side); 02-side rename DEFERRED to the 02 reviser.**
§5(b) names the recommended wiring explicitly (push: watcher thread →
`interrupt(source="sensor")`; pull: `on_action_complete` closure over
`last_report()`/generation counter) and corrects the temporal framing
(next-action key scope, not "the running action"). 02 dropping
"interruption checker" for this seam is flagged for the 02 reviser.

**Attack 14 — over-engineering cut list. → RESOLVED** per the
coordinator's binding cut list: `next_action` cut as carried data
(kept as a read-only convenience property on the error);
`continue_with` cut (the §2.5 example shows the `continue_plan`
one-liner it wrapped — no signature to drift); `interrupt_when` cut;
`source` constrained to the literal. `step_index`/`plan_length` were
*added* as carried data — the one deliberate deviation from the cut
impulse, justified in writing: design 06 needs them to render an
interruption it cannot otherwise index (before-first-action has no
`action_started`), and deriving them client-side is exactly the
tea-leaf reading attack 1 condemned.

No attack was silently dropped. Nothing above re-litigates the
coordinator's rulings.

---

## 9. Producer contract — binding, exhaustive

The 06 (why-diagnostics) reviser builds against this section verbatim.
Anything not listed here is not promised.

### 9.1 Hook event

- **Name:** `on_execution_interrupted`. Registered via
  `planner.register_hook("on_execution_interrupted", callback)`; unknown
  names still raise `ValueError` (existing behavior).
- **Firing rule:** fired **exactly once** per interrupted run, on every
  interruption path (flag honored at pre-loop / per-step / post-loop
  boundaries, and the `asyncio.CancelledError` path). Fire-then-raise:
  the hook fires with the kwargs below, then the machinery raises
  (`PlanInterruptedError`, or the original `CancelledError` on the
  cancellation path). Never fired alongside `on_execution_complete` or
  `on_execution_failed` for the same run. Never fired for a run that
  completed, failed, or was never interrupted.
- **Exact kwargs** (no more, no fewer):

| Kwarg | Type | Meaning |
|---|---|---|
| `reason` | `str` | Free-form, non-empty human reason (e.g. `"enemy_sighted"`). |
| `source` | `InterruptSource` | Closed literal: `"manual"`, `"sensor"`, `"watchdog"`, `"budget"`, `"arbitration"`, `"confidence"`, `"cancellation"`. |
| `state` | `WorldState` | Defensive deep copy of the partial state at the boundary. The **same object** as `err.state`. Always fully-applied and consistent (atomicity axiom). |
| `executed_actions` | `list[str]` | Fresh list, action-name strings, execution order. `[]` if interrupted before the first action. |
| `remaining_actions` | `list[str]` | Fresh list, action-name strings, plan order, never started. `[]` if interrupted after the last action. |
| `step_index` | `int` | Index of the next unstarted action; always `== len(executed_actions)`. |
| `plan_length` | `int` | Total number of plan steps. |
| `state_diff` | `dict[str, tuple[Any, Any]]` | `initial_state_snapshot.diff(state_at_interrupt)`; each value is `(before, after)`. `{}` when nothing changed. |
| `interrupted_at` | `float` | Wall-clock `time.time()` at the moment of interruption. |

- **Hook-error precedence:** on the normal interrupt path, a raising
  hook's exception propagates and supersedes the `PlanInterruptedError`
  (same rule as `on_execution_failed` today). On the cancellation path,
  a raising hook is captured and reported through the planner's logger;
  the `CancelledError` propagates unchanged.

### 9.2 Exception

- **Type:** `class PlanInterruptedError(ValueError)` — defined in
  `goapauto/models/execution.py`, re-exported from `goapauto`.
  **Not** a subclass of `PlanExecutionError` (sibling under `ValueError`).
- **Constructor fields** (keyword-only after `message`): `state`,
  `executed_actions`, `remaining_actions`, `step_index`, `plan_length`,
  `state_diff`, `reason`, `source` — same semantics as the hook kwargs
  table above. `reason` must be non-empty (`ValueError` otherwise);
  `source` must be a literal member (`ValueError` otherwise).
- **Read-only convenience property:** `next_action: str | None`
  (`remaining_actions[0]` if non-empty, else `None`). Not carried data.
- **Raised by:** `PlanExecution.run()` / `PlanExecution.arun()` when an
  interrupt is honored. **Never** raised by `execute_plan` /
  `async_execute_plan`.
- **Catch guidance:** catch `PlanInterruptedError` before
  `PlanExecutionError` when the distinction matters; catch
  `(PlanInterruptedError, PlanExecutionError)` for "did not reach the
  goal" uniformity. Existing `except PlanExecutionError` handlers are
  behaviorally unchanged.

### 9.3 Handle API (`PlanExecution`)

```python
planner.begin_execution(initial_state: WorldState,
                        plan: Plan | list[Action] | PlanResult,
                        goal: Goal | None = None) -> PlanExecution
```
Eager container/step type validation (`TypeError`/`ValueError`, fail
fast); steps normalized to name strings; deep copy of `initial_state`
deferred to `run()`/`arun()` start.

```python
execution.interrupt(reason: str, *, source: InterruptSource = "manual") -> bool
```
Validates (`ValueError` on empty/whitespace reason or unknown source;
validation precedes the terminal check), then under the handle lock:
terminal handle → `False`; already interrupted → `False`; else records
`(reason, source)`, sets the flag, returns `True`. Thread-safe.

```python
execution.run() -> WorldState
execution.arun() -> WorldState
```
Single-shot (`RuntimeError` on second call). `run()` on the calling
thread; `arun()` requires a running event loop. Raises
`PlanInterruptedError` on honored interrupt; `CancelledError`
propagates unchanged from a cancelled `arun()` (handle marked
`interrupted`, `source="cancellation"`, hook fired).

Inspection (all thread-safe snapshots; terminal-state mapping
`is_running`/`is_complete`/`is_interrupted` per §3):
`is_running: bool`, `is_complete: bool`, `is_interrupted: bool`,
`executed_actions: tuple[str, ...]`, `remaining_actions: tuple[str, ...]`,
`state: WorldState` (deep copy), `interrupt_reason: str | None`,
`interrupt_source: InterruptSource | None`.

### 9.4 Check order (normative)

Pre-loop: flag set → interrupt path. Per step: (1) resolve (`KeyError`
unchanged); (2) flag set → interrupt path (before applicability);
(3) applicability → existing failure path; (4) `on_action_start` →
handler/`apply` → `on_action_complete`. Post-loop: flag set →
interrupt path (`remaining_actions == []`, `step_index == plan_length`);
else `on_execution_complete` → return state. Terminal transitions per
§3's state machine.

### 9.5 `InterruptSource`

`Literal["manual", "sensor", "watchdog", "budget", "arbitration",
"confidence", "cancellation"]`, defined in
`goapauto/models/execution.py`, re-exported from `goapauto`.
