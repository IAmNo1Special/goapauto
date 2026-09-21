# Design 01 — Runtime & Replan Budgets

**Status:** implemented (Phase 2, 0.6.0). Planner `time_budget` + `ReplanPolicy`.
**Scope:** per-tick planning time budgets (`time_budget`) and replan
throttling policy. Builds on `Planner.generate_plan` /
`continue_plan` / `async_generate_plan` and `PlanStats` as they exist
in v0.5.0.
**Revision:** v2 — post red-team review. Changes from v1 are marked
`[v2]` where they alter a contract; see §8 for the full
attack-by-attack disposition and §9 for the binding producer contract.

## 1. Goal and non-goals

### Goal

Give a game/agent loop two runtime controls it needs to stay inside a
frame budget:

1. **Per-call planning time budget** — `generate_plan(...,
   time_budget=...)` aborts the A\* search when the wall-clock deadline
   passes and reports the miss explicitly instead of hanging the tick.
2. **Replan throttling** — a small stateful `ReplanPolicy` that decides
   *when* to replan (plan invalid, goal changed, world changed beyond a
   threshold, minimum interval between replans, circuit breaker on
   replan storms `[v2]`) and replans through the existing
   `continue_plan(executed_actions=...)` rather than duplicating it.

### Non-goals (what we choose NOT to build and why)

1. **No full game-loop / agent-tick runner.** Out of scope for a
   planning library. Soulscape owns its loop; a runner would drag in
   threading, scheduling, and execution policy the library deliberately
   avoids (`Planner` is documented not thread-safe).
2. **No per-action execution timeouts.** `[v2 — rationale rewritten]`
   Execution handlers are user code, and this design covers
   *planning-time* budgets only. Feasibility is not the reason: design
   03's cooperative interruption (`execution.interrupt(...)`) makes a
   tick-budget watchdog possible without threads. Ownership is the
   reason: the **host** (Soulscape) owns the tick budget and wires 03's
   mechanism (`execution.interrupt(reason="tick budget exhausted",
   source="budget")`); 01 defines no tick budget, no execute-phase
   auto-interrupt, and no background threads. Terminology: 01's
   `time_budget` is per *planning call*; 03's "tick budget" is per
   *tick* — different budgets, different owners.
3. **No partial / best-effort plan on budget exhaustion.** Returning a
   path that does not achieve the goal would violate `PlanResult`'s
   contract (`plan` achieves the goal, `[]` means already satisfied,
   `None` means no plan). A deceptive partial is worse than an explicit
   miss. Considered and rejected; see Open Questions for the one
   scenario that could reopen it. §2.5 now tells the host what to do
   instead of idling.
4. **No background replanning threads.** The planner is not thread-safe;
   the host drives the loop and calls the policy between actions.
5. **No adaptive budget tuning** (learning budgets from history).
   Premature — measure first, then decide if a tuner earns its place.
6. **No change to search semantics when no budget is given.** The
   default path must be bit-for-bit the current behavior with no added
   per-node overhead.

## 2. User-side API

### 2.1 `time_budget` on the planner

```python
result = planner.generate_plan(
    state,
    goal,
    max_depth=10,
    time_budget=0.05,          # seconds of wall-clock search time; None = unlimited
)
if result.plan is None and planner.stats.budget_exhausted:
    # search was cut off by the deadline — fall back, don't treat as "unsolvable"
    ...

# also available on the other two entry points
result = planner.continue_plan(state, goal, executed_actions=["a", "b"], time_budget=0.05)
result = await planner.async_generate_plan(state, goal, time_budget=0.05)
```

Validation is fail-loud, and runs as the **first statement** of each
entry point — before `_print_header`, before `start_time`, before any
search work — so a bad budget never prints a planning banner or starts
a clock `[v2]`:

```python
planner.generate_plan(state, goal, time_budget=0)      # ValueError
planner.generate_plan(state, goal, time_budget=-1.0)   # ValueError
planner.generate_plan(state, goal, time_budget=float("nan"))  # ValueError
planner.generate_plan(state, goal, time_budget="fast") # TypeError
planner.generate_plan(state, goal, time_budget=True)   # TypeError [v2: bool rejected]
```

The shared validator: `None` → unlimited; `bool` → `TypeError`
(`True` is not "1 second"); non-`(int, float)` → `TypeError`;
`NaN`, `<= 0` → `ValueError`; `+inf` → allowed, behaves as unlimited.
`ReplanPolicy(time_budget=...)` applies the identical validator eagerly
at construction (fail-fast, not fail-at-replan) `[v2]`.

New public helper on `Planner` (needed by the replan policy, useful on
its own):

```python
action = planner.get_action("pickup_key", state)             # Action | None
action = planner.get_action("pickup_key", state, goal=goal)  # goal keyword-optional [v2]
```

This is the public form of the existing private `_get_action_by_name`;
pure addition, no behavior change. `goal` is keyword-optional exactly
like the private method. **Cost note `[v2]`:** the lookup re-queries
providers (`provide_actions(state, goal)`) until a name match. The
policy calls it every tick (step 3, §3.4); with a dynamic
(e.g. model-backed) provider that can cost a provider call per tick.
This is deliberate — provider action sets may legitimately change
between ticks, so the check is always fresh — but hosts with expensive
providers should know the per-tick price.

### 2.2 `PlanStats` extension

```python
@dataclass
class PlanStats:
    nodes_expanded: int = 0
    nodes_visited: int = 0
    plan_length: int = 0
    total_cost: float = 0.0
    execution_time: float = 0.0
    budget_exhausted: bool = False      # NEW (v1) — appended with default: backward compatible
    budget_limit: float | None = None   # NEW [v2] — the time_budget in effect for this call
```

`budget_limit` records the `time_budget` the call ran under (`None` =
unlimited). `execution_time` is then the *consumed* side of the pair —
together they give the diagnostics layer `{limit, consumed}` without a
second channel (§9). Both fields are appended with defaults, so the
extension stays backward compatible.

`PlanResult` is **not** extended: `plan is None` already signals "no
plan", and `planner.stats` is the structured, programmatic channel (it
also rides along into the hook payloads). Adding the flag to both types
would create two sources of truth that can diverge.

### 2.3 New hook event

`on_budget_exhausted` is added to the hook registry in
`Planner.__init__` (no callbacks registered by default, so existing
behavior is unchanged). It fires with **exactly** `(stats=stats)` —
the same kwarg shape as the existing `on_search_failed(stats=...)`
`[v2: payload unified; the v1 `plan=None` kwarg is dropped]` — right
after `on_search_failed` when the deadline — not `max_iterations` —
stopped the search. `register_hook("on_budget_exhausted", cb)` follows
the existing fail-loud hook contract (unknown event names still raise
`ValueError`; hook errors still propagate).

**Migration note — `on_search_failed` now also fires on budget
exhaustion `[v2]`.** Previously that hook meant "the search genuinely
failed" (frontier empty / iterations exhausted). A host that opts into
`time_budget` *and* keeps a legacy `on_search_failed` hook will now see
it fire on timeouts too. The discriminator is the new
`stats.budget_exhausted` flag: a timeout is not "unsolvable", it is
"too slow for this budget". The default path (`time_budget=None`) is
unchanged — the broadening only affects callers who opt into budgets.
Both hooks firing (in `on_search_failed` → `on_budget_exhausted`
order) is deliberate and documented here, not incidental.

### 2.4 `ReplanPolicy`

New module `goapauto/models/replan.py`, exported through
`goapauto/__init__.py` (`__all__` += `ReplanPolicy`, `ReplanDecision`,
`ReplanReason`).

```python
from goapauto import Planner, ReplanPolicy, ReplanReason

policy = ReplanPolicy(
    planner,
    min_replan_interval=1.0,      # seconds between optional replans; 0.0 = off
    replan_on_invalid=True,       # next action's preconditions fail -> replan
    replan_on_goal_change=True,   # arbitrator picked a different goal -> replan
    watched_keys=frozenset({"enemy_visible", "health"}),  # None = world-change trigger off
    max_sensor_age=5.0,           # None = don't gate on sensor staleness
    time_budget=0.05,             # passed through to planner calls
    max_replans_per_tick=None,    # [v2] within-tick cap; needs tick_id (see §3.4)
    max_consecutive_unthrottled_replans=None,  # [v2] circuit breaker; None = off
    reuse_prefix_on_invalid=True, # [v2] False -> fresh generate_plan on invalid/world-changed
    clock=time.monotonic,         # injectable for tests
)

# --- adopt the plan you already have (or skip if you plan through the policy) ---
policy.adopt(state, goal)   # [v2] seeds snapshot/goal/clock; required before should_replan

# --- in the agent loop, between actions ---
decision = policy.should_replan(state, goal, result, next_index=i, tick_id=tick)
if decision.replan:
    result = policy.replan(state, goal, decision)
    plan = result.plan
else:
    # keep executing plan[i]; optionally log decision.reason (e.g. THROTTLED)
    ...

# --- after each executed action ---
policy.note_executed(plan[i])
```

`ReplanDecision` is a frozen dataclass:

```python
@dataclass(frozen=True)
class ReplanDecision:
    replan: bool
    reason: ReplanReason
    detail: dict[str, Any] = field(default_factory=dict)
```

`detail` values are restricted to JSON-safe scalars (`str`, `int`,
`float`, `bool`, `None`, and lists thereof) — the diagnostics layer
serializes decisions, so the offending action is recorded as its
**name string**, never the `Action` object `[v2]`. The dataclass is
frozen for value semantics but **not hashable** (the dict field);
equality works, `hash()` does not — documented, not worked around.

`ReplanReason` is `class ReplanReason(str, Enum)` `[v2]` — the wire
form is the plain string value, which is also what the diagnostics
layer's string-typed fields consume (`decision.reason.value`):

| Value (wire string) | Meaning                                                        |
| ------------------- | -------------------------------------------------------------- |
| `no_plan`           | no current plan — must plan; interval does not apply           |
| `goal_satisfied`    | goal already satisfied — nothing to do                         |
| `plan_valid`        | plan still good — keep executing                               |
| `plan_invalid`      | next action not applicable in current state — replan now       |
| `goal_changed`      | selected goal differs from the planned-for goal — replan now   |
| `world_changed`     | watched keys changed since last plan — replan (throttled)      |
| `throttled`         | would replan, but a throttle/circuit-breaker said no — skip    |
| `sensors_stale`     | would replan on world change, but sensor data is too old — skip|

`[v2: `low_confidence` removed — it had no producer in any of the six
designs (see §5(c)). The enum keeps eight members.]`

`should_replan` accepts the plan as `Plan | PlanResult | None` `[v2]` —
a `PlanResult` is unwrapped to its `.plan`; anything else raises
`TypeError`. (Passing a bare `PlanResult` used to silently index the
NamedTuple's fields — fail-loud replaces that trap.) It also accepts
optional keyword inputs owned by the other differentiators:

```python
decision = policy.should_replan(
    state, goal, plan, next_index=i,
    sensor_age=2.5,          # seconds since last good sensor data; None = unknown/fresh
    was_interrupted=False,   # [v2] design-03 interruption landed here -> force PLAN_INVALID
    tick_id=tick,            # [v2] opaque tick context, passed through to detail/hooks
    emit_hooks=True,         # [v2] False -> decide without firing on_replan_skipped
)
```

`ReplanPolicy` hook events: `on_replan(decision=..., result=...)`,
fired by `replan()` after planning `[v2: moved out of should_replan so
a decision that is never acted on emits nothing]`; and
`on_replan_skipped(decision=...)`, fired by `should_replan` when it
returns `replan=False` with reason `THROTTLED` or `SENSORS_STALE`
(never for `PLAN_VALID`/`GOAL_SATISFIED` — steady-state noise — and
never when `emit_hooks=False`). Same fail-loud contract as planner
hooks.

### 2.5 Host fallback guidance on budget exhaustion `[v2]`

"Fall back" in §2.1 needs a real answer, or the host's most likely
behavior is "retry immediately with a bigger budget" — the replan storm
of §3.4 with extra steps:

- **First plan, budget exhausted** (`NO_PLAN` territory): treat the
  outcome as *unknown*, never as *unsolvable*. Safe-idle or run the
  degraded behavior; do not spin.
- **Replan, budget exhausted**, trigger was `WORLD_CHANGED`: the old
  plan was never shown structurally invalid — the host **may** keep
  executing it (at its own risk; the world did change), or idle. This
  is the one case where "keep the old plan" is defensible.
- **Replan, budget exhausted**, trigger was `NO_PLAN` /
  `PLAN_INVALID` / `GOAL_CHANGED`: there is no usable old plan. Idle
  safely.
- **Never** retry-with-bigger-budget in a tight loop. If the host
  retries at all, back off, and set
  `max_consecutive_unthrottled_replans` so the policy itself refuses
  the storm.

## 3. Precise semantics

### 3.1 Deadline mechanics

- `deadline = time.monotonic() + time_budget`, computed once in
  `generate_plan` / `continue_plan` / `async_generate_plan` and passed
  into `_find_plan` / `_async_find_plan` as `deadline: float | None`.
- Check placement: top of the search-loop body, before the pop —
  `if deadline is not None and time.monotonic() >= deadline: break` —
  **and** after each action expansion's `action.apply(...)` /
  `await action.async_apply(...)` `[v2]`. A single slow expansion (a
  model-backed provider, a slow hook) can otherwise overrun the budget
  by seconds while the spec claims a 50 ms bound; the post-apply check
  keeps the guarantee meaningful under exactly the conditions where it
  matters. Per-expansion cost is one extra `monotonic()` call (~tens of
  ns) against a ~3 µs/node expansion cost; negligible. When
  `time_budget is None` both branches are skipped entirely — the default
  search path gains no overhead and no behavior change.
- On deadline break, `_find_plan` sets
  `self.stats.budget_exhausted = True` and returns `([], None)`.
  `_finalize_plan_generation` branches on the flag:
  - message: `"⌛ Time budget exhausted (0.050s) before a plan was
    found."` (existing ❌/✅ message style kept for the other paths),
  - `PlanResult(plan=None, message=...)`,
  - fires `on_search_failed` (existing semantics: search produced no
    plan — see the migration note in §2.3), then `on_budget_exhausted`.
- `continue_plan` checks `self.stats.budget_exhausted` after its
  `_find_plan` call and returns the same budget-exhausted result shape
  (no prefix stripping — there is no fresh plan to strip from).
- Accepted imprecision (documented, not fixed): the check runs at loop
  boundaries and post-apply only. A slow hook firing *between* those
  points, or an `async_apply` that never yields, still overruns; the
  search stops at the next check. This is inherent to cooperative
  deadlines — the alternative (preemptive cancellation) would violate
  the no-threads constraint.

### 3.2 Composition of `time_budget`, `max_iterations`, `max_depth` `[v2]`

The three bounds are independent hard stops on orthogonal axes —
**depth** (`max_depth`, per-call), **count** (`max_iterations`,
constructor-level), **wall-clock** (`time_budget`, per-call). There is
no precedence hierarchy to memorize; whichever triggers first wins:

```python
while frontier and iteration < self.max_iterations:   # count bound
    if deadline is not None and time.monotonic() >= deadline:  # wall-clock bound
        self.stats.budget_exhausted = True
        break
    ...  # nodes deeper than max_depth are never expanded (depth bound)
```

- `max_depth`: bounds expansion only. It never sets
  `budget_exhausted` and never produces a message — it is not a
  stop-with-signal, it is a shape bound on the search.
- `max_iterations` exhausted first → existing message
  (`"❌ No valid plan found..."`), `budget_exhausted=False`. Unchanged.
- Deadline hit first → budget message, `budget_exhausted=True`.
- Tie within a single iteration (both would fire): the deadline check
  runs first in the loop body, so `time_budget` wins ties.
  Deterministic and testable.
- Both unset/`None` → current behavior exactly.

### 3.3 Edge cases

- `time_budget` given but goal already satisfied → early return
  `plan=[]` before any search; budget irrelevant, `budget_exhausted`
  stays `False`, `budget_limit` still records the passed budget.
- Absurdly small budget (expires before the first pop) → immediate
  break, `plan=None`, `budget_exhausted=True`, `execution_time ≈ 0`.
  This is a valid, testable outcome, not an error.
- `time_budget=float("inf")` → behaves as unlimited (deadline is
  `inf`; the comparison never fires). Allowed, not special-cased.
- `execution_time` (existing stat, `time.time()`-based) keeps its
  meaning; on budget exhaustion it will read ≈ `time_budget`.
  **Clock-basis note `[v2]`:** the deadline uses `time.monotonic()`
  while `execution_time` uses `time.time()`. Under an NTP step the two
  can disagree, so tests assert on `budget_exhausted` + message +
  `budget_limit`, never on an `execution_time <= time_budget + slack`
  inequality (flaky by construction).
- `get_search_graph()` after a budget-exhausted search returns the
  partial graph — useful for why-diagnostics; no change needed.
- `async_generate_plan`: same semantics; the deadline checks are
  synchronous and cheap, so no extra awaits are introduced.

### 3.4 `ReplanPolicy` semantics

**Bookkeeping.** The policy stores: `last_plan_time` (from `clock`),
`last_goal` (deep copy of the `Goal`), `state_snapshot`
(`WorldState.model_copy(deep=True)` at plan/adopt time), `executed:
list[str]`, `_adopted: bool`, `_pending_state` / `_pending_goal` (the
objects the last decision was computed from), `_replans_this_tick:
int`, `_last_tick_id`, `_last_replan_tick_id`,
`_unthrottled_streak: int`, `_circuit_open: bool`, and the config.
`reset()` clears all of it (including `_adopted` — after `reset()` the
policy must be adopted again).

**`adopt(state, goal)` `[v2]`** — the first-call primitive. Seeds
`state_snapshot`, `last_goal`, `last_plan_time = clock()`, clears
`executed`, resets the streak/circuit/tick counters, sets
`_adopted=True`. Use it when the host already holds a plan the policy
didn't create (the normal Soulscape integration path). `replan()` also
adopts implicitly after planning. `state` must be a `WorldState` and
`goal` a `Goal` (dicts are not accepted — `TypeError`); snapshots need
real model instances.

**`should_replan(state, goal, plan, next_index=0, *, sensor_age=None,
was_interrupted=False, tick_id=None, emit_hooks=True)`** evaluates in
this fixed order:

0. **Adoption gate.** If not `self._adopted` → `ValueError("policy
   has no adopted plan: call adopt() or replan() first")`. No phantom
   `GOAL_CHANGED`, no `diff`-against-`None` crash — fail-loud instead.
   (`state`/`goal` must be `WorldState`/`Goal`; `plan` must be
   `Plan | PlanResult | None` — a `PlanResult` is unwrapped, anything
   else is `TypeError`; `next_index < 0` is `ValueError`.)
   **Tick accounting.** If `max_replans_per_tick` is configured and
   `tick_id is None` → `ValueError` (a per-tick cap without tick
   context is a host contract violation). If `tick_id is not None`
   and differs from `_last_tick_id` → `_replans_this_tick = 0`,
   `_last_tick_id = tick_id`.
1. `goal.is_satisfied(state)` → `(False, GOAL_SATISFIED)`.
2. `plan is None` or `next_index >= len(plan)` → trigger `NO_PLAN`.
   The interval never blocks the first plan — but the circuit breaker
   (below) still applies from the second consecutive unthrottled
   replan onward.
3. **Next-action validity** (only if `replan_on_invalid`), or forced by
   `was_interrupted` `[v2]`: look up `plan[next_index]` via
   `planner.get_action(name, state, goal=goal)`; if `was_interrupted`
   or the action is missing or `not action.is_applicable(state)` →
   trigger `PLAN_INVALID`, `detail = {"action": name_or_None,
   "interrupted": was_interrupted}`. Bypass the interval: a broken
   plan cannot execute, and throttling it would only hide a livelock
   (fail-loud philosophy). An interrupted-but-unstarted action is
   usually still applicable — the interruption is a *control signal*,
   not a state fact, so `was_interrupted` forces the trigger
   regardless of applicability.
4. **Goal change** (only if `replan_on_goal_change`): `goal !=
   self._last_goal` → trigger `GOAL_CHANGED`. Bypass the interval;
   the old plan was built for a different objective. (Pydantic `Goal`
   equality compares field values; callable `target_state` entries
   compare by identity — documented caveat.)
5. **Staleness gate** (only if `max_sensor_age` is set and
   `sensor_age is not None`): `stale = sensor_age > max_sensor_age`.
6. **World change** (only if `watched_keys` is not `None`):
   `changed = set(state.diff(self._state_snapshot)) & watched_keys`;
   if non-empty →
   - if `stale` → `(False, SENSORS_STALE, {"sensor_age": ...})`
   - elif `clock() - last_plan_time < min_replan_interval` →
     `(False, THROTTLED, {"throttled_by": "min_replan_interval",
     "changed_keys": sorted(changed), "elapsed": ...,
     "cooldown_remaining_ms": ...})`
   - else → trigger `WORLD_CHANGED`, `detail =
     {"changed_keys": sorted(changed)}`.
   
   Precedence `[v2]`: `SENSORS_STALE` is evaluated **before**
   `THROTTLED`. Staleness suppresses the replan regardless of the
   interval, so it is the dominant, more actionable signal; reporting
   "too soon" when the data is garbage would mislead the host.
7. Otherwise → `(False, PLAN_VALID)`.

**Suppression order for a would-be replan trigger** `[v2]` —
circuit breaker, then per-tick cap (the interval was already handled
at step 6 for `WORLD_CHANGED`; the structural triggers bypass it by
design):

- **Circuit breaker.** If `_circuit_open` and the trigger is
  `NO_PLAN`, `PLAN_INVALID`, or `GOAL_CHANGED` → `(False, THROTTLED,
  {"throttled_by": "circuit_breaker",
  "consecutive_unthrottled_replans": streak,
  "last_replan_tick_id": ...})`. `WORLD_CHANGED` is still allowed as
  a probe while the circuit is open (it passed its own interval
  check — the world genuinely moved).
- **Per-tick cap.** If `max_replans_per_tick` is set and
  `_replans_this_tick >= max_replans_per_tick` → `(False, THROTTLED,
  {"throttled_by": "max_replans_per_tick", "replans_this_tick": ...,
  "max_replans_per_tick": ...})`. Applies to every trigger —
  including the unthrottled ones. This is the knob doc 06's
  `replans_per_tick` budget name refers to.
- **Steady-state reset.** If the final decision is `(False,
  PLAN_VALID)` or `(False, GOAL_SATISFIED)` → `_unthrottled_streak =
  0`, `_circuit_open = False`. The breaker measures *consecutive ticks
  without a usable plan* — the livelock metric — not mere replan
  count.

Every decision records `tick_id` in `detail` (when provided) and
`_pending_state`/`_pending_goal` for the `replan()` identity check.
Hook emission: if `emit_hooks` and the decision is `(False,
THROTTLED)` or `(False, SENSORS_STALE)` → `on_replan_skipped(decision=
decision)`. (`PLAN_VALID`/`GOAL_SATISFIED` never emit; `emit_hooks=
False` gives a side-effect-free preview — calling `should_replan`
twice otherwise fires the skip hook twice, which is documented, not
accidental.)

**Circuit-breaker dynamics** `[v2]` (Ruling F):

- `max_consecutive_unthrottled_replans: int | None = None`
  (constructor; `None` = off; must be `>= 1`, `bool` rejected).
- `replan()` increments `_unthrottled_streak` when the decision's
  reason is `NO_PLAN`, `PLAN_INVALID`, or `GOAL_CHANGED` — the
  triggers that bypass `min_replan_interval`. `WORLD_CHANGED` replans
  never increment it (they are interval-throttled already).
- When the streak reaches the threshold, the circuit **opens**: the
  next unthrottled trigger is answered `THROTTLED` instead of
  replanning. The open circuit is a hard stop, not a backoff curve —
  deterministic and testable.
- The circuit **closes** (streak reset) on: a `(False, PLAN_VALID)`
  or `(False, GOAL_SATISFIED)` decision (the agent has a usable plan
  again), `adopt()`, or `reset()`. A successful `WORLD_CHANGED` probe
  replan also closes it.
- Recommended host default when the host has no guard of its own:
  `max_consecutive_unthrottled_replans=5`. The library default stays
  `None` (everything opt-in, §3.5).
- What it stops: the attack-7 livelock — unsolvable goal, or a
  precondition false every tick, burning a full `time_budget` replan
  every tick forever. What it does *not* stop: an alternating
  `PLAN_INVALID` / `PLAN_VALID` flap (each invalid tick produces a
  fresh, valid plan and the agent executes on the valid ticks). That
  flap is bounded by `time_budget` per replan and the agent is making
  progress; see Open Questions.

**`replan(state, goal, decision, tick_id=None)`** `[v2]` re-checks
nothing about the world; it validates the *call*, then acts:

- `decision.replan is False` → `ValueError` (replanning on a
  don't-replan decision is a host bug — fail-loud).
- `decision.reason` not in `{NO_PLAN, PLAN_INVALID, GOAL_CHANGED,
  WORLD_CHANGED}` → `ValueError` (defensive; unreachable via
  `should_replan`).
- `state is not self._pending_state` or `goal is not
  self._pending_goal` → `ValueError`. The decision was computed
  against specific objects; replanning against different ones would
  poison `state_snapshot` / `last_goal` and the `continue_plan`
  prefix. The host must pass the same objects (or call
  `should_replan` again).
- Tick attribution: effective tick is `tick_id` if given, else
  `decision.detail.get("tick_id")`; roll the per-tick counter on
  boundary, increment `_replans_this_tick`, record
  `_last_replan_tick_id`.
- Dispatch:
  - `PLAN_INVALID` / `WORLD_CHANGED` → `planner.continue_plan(state,
    goal, executed_actions=self._executed,
    time_budget=self._time_budget)` — **unless**
    `reuse_prefix_on_invalid=False`, in which case a fresh
    `planner.generate_plan(state, goal, time_budget=...)`. `[v2]`
    Prefix reuse strips a leading run by *order*, not by state: in
    domains with cyclic or toggle actions the stripped prefix may
    drop a step the fresh plan needs (e.g. executed `["toggle"]`,
    fresh plan `["toggle", "press"]` → stripped to `["press"]`).
    The default keeps the old behavior; hosts with cyclic domains
    set the flag to `False`.
  - `GOAL_CHANGED` / `NO_PLAN` → fresh `planner.generate_plan(state,
    goal, time_budget=self._time_budget)`; the executed prefix is
    meaningless against a new goal (or untrusted plan).
- After planning: `last_plan_time = clock()`,
  `state_snapshot = state.model_copy(deep=True)`,
  `last_goal = goal.model_copy(deep=True)`, `executed = []`
  (implicit adoption — `_adopted` stays `True`).
- Circuit accounting: unthrottled-trigger replan → streak += 1, open
  the circuit at threshold.
- Fires `on_replan(decision=decision, result=result)`; returns the
  `PlanResult`.

**`note_executed(action_name: str)`** appends to `executed`
(`TypeError` on non-`str`). The policy does not execute anything — no
game loop, no runner (non-goal 1). Hosts using design 03's
`PlanExecution` handle bridge `PlanInterruptedError.executed_actions`
into `note_executed` calls (see §5(b)).

### 3.5 Defaults — everything opt-in

| Parameter | Default | Effect when default |
|---|---|---|
| `time_budget` (planner calls) | `None` | unlimited; zero behavior change |
| `PlanStats.budget_exhausted` | `False` | — |
| `PlanStats.budget_limit` | `None` | — |
| `ReplanPolicy.min_replan_interval` | `0.0` | no throttling |
| `replan_on_invalid` / `replan_on_goal_change` | `True` | the two structural triggers |
| `watched_keys` | `None` | world-change trigger off |
| `max_sensor_age` | `None` | staleness gating off |
| `ReplanPolicy.time_budget` | `None` | replans run unlimited |
| `max_replans_per_tick` | `None` | no per-tick cap (requires `tick_id` when set) |
| `max_consecutive_unthrottled_replans` | `None` | circuit breaker off |
| `reuse_prefix_on_invalid` | `True` | `continue_plan` prefix reuse (see cyclic hazard, §3.4) |

## 4. Failure-mode table

| Failure mode | Handling |
|---|---|
| `time_budget <= 0`, `NaN`, non-numeric, or `bool` | `TypeError` / `ValueError` at call time, as the first statement — before `_print_header` (fail-loud) |
| Deadline hit mid-search | `plan=None`, message names the budget, `stats.budget_exhausted=True`, `stats.budget_limit` set; `on_search_failed` + `on_budget_exhausted` fire. No exception — "no plan within budget" is a normal outcome, not an error |
| `max_iterations` + `time_budget` both set | orthogonal axes; first trigger wins, ties go to the deadline (it is checked first); flag + message identify which |
| `max_depth` also set | bounds expansion only; never sets the flag, never a "winner" |
| Slow hook / provider / awaited coroutine overruns deadline | deadline also checked post-`apply`; residual overrun at check boundaries is accepted (cooperative deadline) |
| `min_replan_interval < 0`, `max_sensor_age < 0`, bad policy `time_budget`, `max_replans_per_tick < 1`, `max_consecutive_unthrottled_replans < 1`, non-bool flags | `ValueError` / `TypeError` at `ReplanPolicy` construction (fail-fast) `[v2]` |
| `should_replan` before `adopt()` | `ValueError` — no `diff`-against-`None` crash, no phantom `GOAL_CHANGED` `[v2]` |
| `should_replan` with `plan=None` | `(True, NO_PLAN)` — interval never blocks the first plan; circuit breaker still applies |
| `should_replan` with a `PlanResult` | unwrapped to `.plan`; other non-list types → `TypeError` `[v2]` |
| `next_index` past end of plan, goal unsatisfied | `(True, NO_PLAN)` — plan is spent; `next_index < 0` → `ValueError` |
| Next action name not found in providers | `(True, PLAN_INVALID)` — fail-loud at replan, not silent skip |
| `replan()` on a `replan=False` decision, or with mismatched state/goal objects | `ValueError` — the host must pass the objects the decision was computed from `[v2]` |
| `executed_actions` prefix does not match fresh plan | existing `continue_plan` behavior: leading run stripped by order; cyclic/toggle domains should set `reuse_prefix_on_invalid=False` `[v2]` |
| Goal with callable `target_state` entries | equality by identity; goal-change detection may miss semantic changes — documented caveat |
| Sensor data stale (`sensor_age > max_sensor_age`) | world-change replans suppressed (`SENSORS_STALE`, evaluated before `THROTTLED`); structural triggers still fire |
| Replan storm (unthrottled triggers every tick) | circuit breaker opens after `max_consecutive_unthrottled_replans` consecutive unthrottled replans → `THROTTLED`; per-tick cap via `max_replans_per_tick` + `tick_id` `[v2]` |
| `max_replans_per_tick` set but no `tick_id` passed | `ValueError` at `should_replan` — the cap needs tick context `[v2]` |
| Hook raises inside policy or budget hooks | propagates (same fail-loud contract as planner hooks) |
| Planner shared across threads with a policy | unsupported — `Planner` is documented not thread-safe; policy inherits that |

## 5. Interaction notes with the other five differentiators `[v2 — rewritten; ownership stated, fiction removed]`

**(a) Sensor caching / staleness.** The policy's `max_sensor_age` /
`sensor_age` gate (§3.4 steps 5–6) is the replan side's view of the
sensor side's staleness. The seam stays provider-free: the policy takes
a plain `float`, never a `SensorReport` or Jev type — and the
**host owns the translation**. Recommended: the max age over the
policy's `watched_keys` (a scalar can't express "3 keys FRESH, 1 STALE";
worst-case age is the conservative scalar). `sensor_age` means
*seconds since last good sensor data* — a fully-gated `{}` from a
fresh judgment call is a *judgment outcome*, not staleness; mapping it
to a large `sensor_age` would wrongly suppress replans. `JevSensor`
degrades to `{}` only when older than its `max_stale` — that is the
case the host maps to a large `sensor_age`. `JevSensor.min_interval`
(sensor-side throttle) and `ReplanPolicy.min_replan_interval`
(plan-side throttle) are independent and compose multiplicatively;
both default off. Tick order: sense → freshness check →
`should_replan` → plan. Note for 02's reviser: 01 builds no per-call
*node* budget (`max_iterations` stays constructor-level), so 02's
"tighter node budget on STALE precondition keys" has no plug in this
design — that gap is 02-owned.

**(b) Action interruption.** This design assumes `should_replan` is
called *between* actions and never interrupts a running one (non-goal
1). The interruption contract is now explicit and implementable
`[v2]`: when design 03's interruption lands (host catches
`PlanInterruptedError`), the host passes `was_interrupted=True` into
`should_replan`, which returns `(True, PLAN_INVALID,
{"action": ..., "interrupted": True})` bypassing
`min_replan_interval`. The v1 claim — "the host calls `should_replan`
after cancellation and gets `PLAN_INVALID`" — was false: an
interrupted-but-unstarted action is usually still applicable, so the
check alone returns `PLAN_VALID` and execution *resumes*. The
`was_interrupted` input is the control signal the policy was missing;
without it the contract is unimplementable. Hosts using 03's
`PlanExecution` handle bridge `PlanInterruptedError.executed_actions`
into `policy.note_executed(...)` so the policy's `_executed` prefix
stays accurate. Execution-phase budgets belong to the host via 03's
`execution.interrupt(reason="tick budget exhausted",
source="budget")` — 01 defines no tick budget (its `time_budget` is
per planning call) and no auto-interrupt mechanism.

**(c) Confidence gating.** `[v2 — removed]` The v1 `plan_confidence`
input / `min_plan_confidence` threshold / `LOW_CONFIDENCE` trigger had
no producer: design 04 gates per *answer* (sensor keys, strategy
picks), never scores a *plan*, and its telemetry has no plan-level
fields. Plan-confidence scoring is unowned across all six designs —
recorded here as future work with no owner, not attributed to (c).
(A side effect of the removal: the v1 §5(c) "budget-exhausted feeds
confidence 0.0" mapping is gone too — it was unreachable anyway, since
`NO_PLAN` short-circuits before any confidence check could run.)

**(d) Provider-independent judgment interface.** The policy never
touches `JevSensor` or any provider type: staleness arrives as
`sensor_age: float | None`. If (d) later offers an "is this state
change significant?" judgment, it plugs in **host-side** — the host
filters the `watched_keys` diff before calling `should_replan`, or
narrows `watched_keys` dynamically. The diff stays the default,
provider-free mechanism; no policy change needed. Budget-accounting
note: `time_budget` covers planner internals only. Sense/judgment
latency (including a judgment's `latency_ms`, which exists only
post-call) is outside every budget 01 enforces — there is no in-flight
judgment hook in this design (accepted, out of scope for Phase 1); a
host-level tick budget is the future owner.

**(e) Why-diagnostics.** Emission points, in order: (1) budget
exhaustion fires `on_search_failed` then `on_budget_exhausted`, both
with `(stats=stats)` carrying `budget_exhausted=True` and
`budget_limit`; the partial `get_search_graph()` remains available for
"how far did it get". (2) Every acted-on replan fires
`on_replan(decision=..., result=...)` from `replan()`;
throttled/stale skips fire `on_replan_skipped(decision=...)` from
`should_replan`. (3) `ReplanDecision.detail` (JSON-safe by contract)
is the structured payload — `changed_keys`, `elapsed`,
`cooldown_remaining_ms`, `sensor_age`, offending `action` name,
`throttled_by`, tick ids. `ReplanReason` is a `str` Enum; its `.value`
is the wire vocabulary. **The 01→06 adapter is specified exactly in
§9** — 01 emits hooks, 06's inspector subscribes and maps; neither
side synthesizes.

## 6. Test plan sketch (100% coverage gate holds)

New tests live in `tests/goapauto/models/test_planner.py` (budget) and
`tests/goapauto/models/test_replan.py` (policy). Every branch below
must be covered; no `pragma: no cover` in source.

**Time budget (planner):**

- default path unchanged: `time_budget=None` plans normally,
  `budget_exhausted is False`, `budget_limit is None`
- validation: `0`, `-1.0`, `NaN` → `ValueError`; `"fast"`,
  `True` → `TypeError`; raised before `_print_header` (capsys: no
  banner on `verbose=True`)
- exhaustion: generous-iteration but tiny-budget domain (e.g. deep
  chain, `time_budget=1e-6`) → `plan is None`, `"budget" in
  message.lower()`, `stats.budget_exhausted is True`,
  `stats.budget_limit == time_budget` (no `execution_time`
  inequality — clock bases differ, §3.3)
- slow-apply overrun: provider whose `apply` sleeps past the budget →
  `budget_exhausted` still `True` (post-apply check)
- `max_iterations` wins when smaller: tiny `max_iterations`, huge
  budget → old message, flag `False`
- budget wins when smaller: huge `max_iterations`, tiny budget → flag
  `True`; tie → budget wins deterministically
- goal already satisfied + budget → `plan == []`, flag `False`,
  `budget_limit` recorded
- hooks: `on_budget_exhausted` fires exactly on exhaustion with kwargs
  `== {"stats"}`; unregistered by default (no-op);
  `register_hook("on_budget_exhausted")` with bad name still raises
  `ValueError`; `on_search_failed` also fires on exhaustion (migration
  coverage)
- `continue_plan` and `async_generate_plan` accept and honor
  `time_budget` (exhaustion shape identical)
- `get_action` returns the action / `None` for unknown names; `goal`
  keyword-optional

**Replan policy:**

- construction: negative `min_replan_interval` → `ValueError`;
  negative `max_sensor_age` → `ValueError`; bad policy
  `time_budget` → `TypeError`/`ValueError` eagerly; `True` for any
  numeric param → `TypeError`; `max_replans_per_tick=0` →
  `ValueError`
- adoption: `should_replan` before `adopt()` → `ValueError`;
  `adopt()` then `should_replan` → no phantom `GOAL_CHANGED`;
  `reset()` → back to `ValueError`
- `NO_PLAN`: `plan=None` → replan even with huge interval;
  `PlanResult` unwrapped; `plan=42` → `TypeError`;
  `next_index` past end → `NO_PLAN`; negative → `ValueError`
- `GOAL_SATISFIED`: satisfied goal → no replan, no planning call
- `PLAN_INVALID`: next action inapplicable (and unknown action name)
  → replan via `continue_plan` (assert via hook/spy), bypasses
  interval; `was_interrupted=True` on an *applicable* action →
  `PLAN_INVALID` with `detail["interrupted"] is True`
- `GOAL_CHANGED`: different goal → fresh `generate_plan`, `executed`
  cleared
- `WORLD_CHANGED`: watched key flips → replan; unwatched key flips →
  `PLAN_VALID`; `watched_keys=None` → never fires
- `THROTTLED`: world change within interval → no replan, reason
  `THROTTLED`, `detail["throttled_by"] == "min_replan_interval"`,
  `cooldown_remaining_ms` present, `on_replan_skipped` fired; after
  interval elapses (fake clock) → replan
- `SENSORS_STALE`: stale age suppresses world-change replan but not
  `PLAN_INVALID`; stale + within-interval → `SENSORS_STALE`
  (precedence over `THROTTLED`)
- circuit breaker: `max_consecutive_unthrottled_replans=2` →
  replans 1–2 proceed, 3rd trigger → `THROTTLED` with
  `throttled_by == "circuit_breaker"`; `PLAN_VALID` tick closes it;
  `WORLD_CHANGED` probe allowed while open
- `max_replans_per_tick=1` + `tick_id`: second replan same tick →
  `THROTTLED`/`max_replans_per_tick`; new `tick_id` → counter reset;
  cap set + `tick_id=None` → `ValueError`
- `replan()` guards: `replan=False` decision → `ValueError`;
  mismatched state/goal objects → `ValueError`
- `reuse_prefix_on_invalid=False` → `PLAN_INVALID` routes to fresh
  `generate_plan`
- `emit_hooks=False` → no hook traffic on preview; default `True`
  fires `on_replan_skipped` on throttled skips
- `on_replan(decision=..., result=...)` fires from `replan()` with
  the result; unknown event → `ValueError`
- `note_executed` accumulates; `replan` clears; `reset()` clears all
- `detail` JSON-safe: `json.dumps(decision.detail)` on every reason
- `ReplanReason` wire values: `ReplanReason.THROTTLED.value ==
  "throttled"` etc.
- fake-clock injection drives all timing assertions (no wall-clock
  flakiness)

## 7. Open questions (recorded, not guessed)

1. **Partial plans on exhaustion.** We return `plan=None`. The one
   scenario that could reopen this: an agent that prefers *any*
   progress over idling (e.g. real-time strategy). If reopened, the
   partial path must be a separate opt-in return shape — never
   smuggled into `PlanResult.plan` under its current contract.
2. **Should `PLAN_INVALID` be gated by sensor staleness?** Proposed no
   (structural trigger), but the precondition check itself reads
   possibly-stale state — if (a) lands a "state trust" signal, revisit.
3. **Naming.** `ReplanPolicy` vs `ReplanController` vs
   `ReplanThrottler`; `watched_keys` vs `replan_keys`;
   `max_consecutive_unthrottled_replans` is a mouthful. Bikeshed at
   implementation time.
4. **Module placement.** Proposed `goapauto/models/replan.py`
   (domain type, next to `goal_arbitrator.py`). Alternative: a new
   `goapauto/loop.py` if a runner ever exists — but that is
   explicitly a non-goal, so `models/` it is.
5. **Flap damping.** The circuit breaker stops *consecutive*
   unthrottled replans, not an alternating `PLAN_INVALID` /
   `PLAN_VALID` flap (each invalid tick yields a fresh valid plan, so
   the streak resets). Each replan is bounded by `time_budget` and the
   agent executes on the valid ticks — is that sufficient, or does the
   flap need its own damper? Left open deliberately.
6. **Plan-confidence scoring** has no producer in any of the six
   designs (see §5(c)). If a future design owns it, the natural plug
   is a new `should_replan` input + trigger — not this design's
   problem to pre-build.

## 8. Adversarial review — disposition of all 23 attacks `[v2]`

From `designs/reviews/01-runtime-replan-budgets-review.md`. Nothing
dropped.

1. **`should_replan` silently mis-indexes a `PlanResult`.**
   **RESOLVED.** Signature is now `plan: Plan | PlanResult | None`;
   `PlanResult` is unwrapped to `.plan`; anything else raises
   `TypeError` (§2.4, §3.4 step 0, test plan).
2. **`replan()` on a don't-replan decision is undefined.**
   **RESOLVED.** `replan()` with `decision.replan is False` raises
   `ValueError`; an unexpected `replan=True` reason raises
   `ValueError` defensively (§3.4, test plan).
3. **`replan()` takes a decision computed from a possibly different
   state.** **RESOLVED.** `should_replan` records the exact
   `state`/`goal` objects; `replan()` raises `ValueError` on identity
   mismatch — the host must pass the same objects or re-decide (§3.4).
4. **Mutable `detail` dict in a frozen dataclass; `ReplanReason`
   type unspecified.** **RESOLVED.** `ReplanReason(str, Enum)` with
   documented wire values; `detail` restricted to JSON-safe scalars
   by contract; `detail["action"]` is the action **name** string,
   never the `Action` object; non-hashability of `ReplanDecision`
   documented (§2.4, §9).
5. **`get_action`'s `goal` parameter vague; per-tick provider cost
   undocumented.** **RESOLVED.** `goal` is keyword-optional like the
   private method; the per-tick provider-call cost is documented in
   §2.1. Per-tick lookup (not plan-time caching) is kept deliberately:
   provider action sets may change between ticks, so the check must
   be fresh.
6. **`time_budget=True` silently accepted as 1 second.**
   **RESOLVED.** `bool` is rejected with `TypeError` in the planner
   validator and in every numeric `ReplanPolicy` parameter (§2.1,
   §4).
7. **No circuit breaker on repeated replan triggers (the big one).**
   **RESOLVED** per coordinator Ruling F: `max_replans_per_tick`
   (within-tick cap, `tick_id`-based) plus
   `max_consecutive_unthrottled_replans` (open-circuit breaker on
   consecutive unthrottled replans; hard stop, not backoff; closes on
   steady state / adopt / reset / successful probe) (§3.4, §4).
8. **`PLAN_INVALID` bypass + flapping precondition = replan storm,
   no damping.** **RESOLVED in part, remainder DEFERRED to Open
   Question 5.** Consecutive unthrottled replans (including
   persistently-false preconditions) trip the circuit breaker.
   Alternating invalid/valid flaps do not — each invalid tick
   produces a usable plan — and are accepted as bounded by
   `time_budget`; whether they need a damper is genuinely open.
9. **Deadline unenforceable against a single slow expansion.**
   **RESOLVED.** The deadline is now also checked after each
   `action.apply(...)` / `await action.async_apply(...)` in the
   expansion loop, and `stats.budget_limit` + `stats.execution_time`
   give the diagnostics layer `{limit, consumed}` (§3.1, §2.2, §9).
10. **`execution_time` vs deadline mix clocks; the proposed test is
    flaky.** **RESOLVED.** The `execution_time <= time_budget +
    slack` assertion is dropped from the test plan; clock bases
    documented; tests assert `budget_exhausted` + message +
    `budget_limit` (§3.3, §6).
11. **Validation order vs. observable side effects.**
    **RESOLVED.** `time_budget` validation is specified as the first
    statement of each entry point, before `_print_header` (§2.1, test
    plan covers via capsys).
12. **Inconsistent validation (`max_sensor_age`, policy
    `time_budget` unvalidated).** **RESOLVED.** All numeric policy
    params validated eagerly in `__init__` (§4, §6).
13. **First-call adoption broken (crash + phantom trigger).**
    **RESOLVED.** New `adopt(state, goal)` primitive seeds
    snapshot/goal/clock; `should_replan` before adoption raises
    `ValueError` instead of crashing in `diff` or hallucinating
    `GOAL_CHANGED`; `reset()` returns to un-adopted (§3.4, test plan
    has the adoption case).
14. **"Prefix reuse is safe" claim false for cyclic/undoable
    actions.** **RESOLVED.** Claim weakened to order-based stripping
    with the toggle counterexample documented; new
    `reuse_prefix_on_invalid=False` routes invalid/world-changed
    replans through fresh `generate_plan` (§3.4, §4).
15. **`THROTTLED` vs `SENSORS_STALE` precedence unspecified.**
    **RESOLVED.** `SENSORS_STALE` is evaluated first (staleness
    dominates the interval — it suppresses the replan regardless, and
    is the more actionable signal). Precedence table in §3.4 step 6.
16. **`on_replan_skipped` fires inside `should_replan` — a query
    with side effects.** **RESOLVED.** `on_replan` moved to
    `replan()` (fires once, with the result, only when a replan
    actually happens); `should_replan` gains `emit_hooks=False` for
    side-effect-free preview; double-fire on repeated calls is
    documented behavior, not accidental (§2.4, §3.4).
17. **`on_search_failed` now fires on budget exhaustion — semantic
    broadening.** **RESOLVED.** Documented as a deliberate,
    prominent migration note (§2.3): opt-in-only broadening,
    discriminable via `stats.budget_exhausted`. Both hooks keep
    firing, loudly.
18. **Hook payload asymmetry.** **RESOLVED.** `on_budget_exhausted`
    now fires with exactly `(stats=stats)`, matching
    `on_search_failed(stats=...)` (§2.3).
19. **`planner.hooks` gains a new key.** **Recorded, no change.**
    Zero behavioral cost when unregistered; noted in §9 for 06's
    inspector.
20. **Non-goal 2 rationale stale relative to doc 03.**
    **RESOLVED.** Rationale rewritten: feasibility conceded (03's
    cooperative interruption needs no threads), ownership assigned
    to the host, tick-budget vs time-budget terminology
    disambiguated (§1, §5(b)).
21. **Non-goal 1 creates a tick-vocabulary vacuum.**
    **RESOLVED in part.** `should_replan`/`replan` accept an opaque
    `tick_id`, passed through into `ReplanDecision.detail` and hooks;
    `max_replans_per_tick` is built on it. 01 still will not *speak*
    tick beyond pass-through — 06 derives the rest via the §9
    adapter. No runner is built (non-goal stands).
22. **Non-goal 3 gives the host nothing to do on exhaustion.**
    **RESOLVED.** New §2.5 host fallback guidance: keep-old-plan only
    defensible for `WORLD_CHANGED`; safe-idle for `NO_PLAN` /
    `PLAN_INVALID` / `GOAL_CHANGED`; never tight-loop retry with a
    bigger budget.
23. **Cut the `ReplanPolicy`; ship the primitive + predicate.**
    **REJECTED, with reasoning.** Coordinator Ruling F builds the
    circuit breaker *on the policy*; doc 06 consumes its events;
    the state it keeps (last plan time, snapshot, goal, executed
    prefix) is exactly what correct throttling needs and no host
    should reimplement per call site. The policy is slimmed per the
    rulings (confidence input removed), not removed. The enum stays
    too: as a `str` Enum its `.value` *is* a producer-owned
    free-form string, which satisfies 06's string-typed fields.

## 9. Producer contract `[v2]` — binding on consumers (esp. design 06)

This section is exact. The 06 reviser builds against it; vagueness
here is a defect.

**Planner hooks** (registered in `Planner.__init__`; new key
`"on_budget_exhausted"` present in `planner.hooks`):

- `on_search_failed(stats=stats)` — existing; kwargs exactly
  `{"stats": PlanStats}`. Now also fires when the deadline (not
  `max_iterations`) stops the search (migration note, §2.3).
- `on_budget_exhausted(stats=stats)` — kwargs exactly
  `{"stats": PlanStats}`. Fires immediately after
  `on_search_failed`, iff the deadline stopped the search.

**`PlanStats` new fields** (appended, defaulted):

- `budget_exhausted: bool = False`
- `budget_limit: float | None = None` — the `time_budget` in effect
  for the call that produced these stats.

**`ReplanPolicy` hooks** (own registry, same fail-loud contract):

- `on_replan(decision=decision, result=result)` — kwargs exactly
  `{"decision": ReplanDecision, "result": PlanResult}`; fired by
  `replan()` after planning.
- `on_replan_skipped(decision=decision)` — kwargs exactly
  `{"decision": ReplanDecision}`; fired by `should_replan` iff
  `replan=False`, `reason ∈ {THROTTLED, SENSORS_STALE}`, and
  `emit_hooks=True`.

**`ReplanPolicy.__init__` parameters** (names, types, defaults):

- `planner: Planner` (required, positional)
- `min_replan_interval: float = 0.0` (`bool`→`TypeError`;
  non-numeric→`TypeError`; `NaN` or `< 0`→`ValueError`)
- `replan_on_invalid: bool = True`, `replan_on_goal_change: bool =
  True` (non-`bool`→`TypeError`)
- `watched_keys: frozenset[str] | None = None`
- `max_sensor_age: float | None = None` (`None` ok; else numeric
  rules as above, `< 0`→`ValueError`)
- `time_budget: float | None = None` (planner validator, eager)
- `max_replans_per_tick: int | None = None` (`None` off; `bool`→
  `TypeError`; non-`int`→`TypeError`; `< 1`→`ValueError`)
- `max_consecutive_unthrottled_replans: int | None = None` (same
  validation as above)
- `reuse_prefix_on_invalid: bool = True` (non-`bool`→`TypeError`)
- `clock: Callable[[], float] = time.monotonic`

**Method signatures:**

- `adopt(state: WorldState, goal: Goal) -> None`
- `should_replan(state: WorldState, goal: Goal, plan: Plan |
  PlanResult | None, next_index: int = 0, *, sensor_age: float |
  None = None, was_interrupted: bool = False, tick_id: int | str |
  None = None, emit_hooks: bool = True) -> ReplanDecision`
- `replan(state: WorldState, goal: Goal, decision: ReplanDecision,
  tick_id: int | str | None = None) -> PlanResult`
- `note_executed(action_name: str) -> None`
- `reset() -> None`
- `Planner.get_action(name: str, state: WorldState, *,
  goal: Goal | None = None) -> Action | None`

**`ReplanDecision` fields:** `replan: bool`, `reason: ReplanReason`,
`detail: dict[str, Any]` (values restricted to JSON-safe scalars;
`detail["action"]` is a name `str`, never an `Action`). Frozen,
not hashable.

**`ReplanReason` wire values** (`str` Enum; consumers use
`decision.reason.value`): `no_plan`, `goal_satisfied`, `plan_valid`,
`plan_invalid`, `goal_changed`, `world_changed`, `throttled`,
`sensors_stale`.

**`detail` keys by reason** (all keys always JSON-safe):

- `NO_PLAN`: `tick_id?`
- `PLAN_INVALID`: `action: str | None`, `interrupted: bool`,
  `tick_id?`
- `GOAL_CHANGED`: `tick_id?`
- `WORLD_CHANGED`: `changed_keys: list[str]`, `tick_id?`
- `THROTTLED`: `throttled_by: "min_replan_interval" |
  "max_replans_per_tick" | "circuit_breaker"`, plus for
  `min_replan_interval`: `changed_keys`, `elapsed: float`,
  `cooldown_remaining_ms: float`; for `max_replans_per_tick`:
  `replans_this_tick: int`, `max_replans_per_tick: int`; for
  `circuit_breaker`: `consecutive_unthrottled_replans: int`,
  `last_replan_tick_id: int | str | None`; always `tick_id?`
- `SENSORS_STALE`: `sensor_age: float`, `tick_id?`
- `PLAN_VALID` / `GOAL_SATISFIED`: `tick_id?`

(`tick_id?` = present iff the host passed one.)

**01→06 adapter** (06-owned code; 01 emits, 06 maps — neither side
synthesizes):

- `budget_exhausted` TraceEvent ← `on_budget_exhausted(stats)`:
  `{budget_name: "time_budget", limit: stats.budget_limit, consumed:
  stats.execution_time, unit: "s", phase: "plan", goal: null,
  action: null}`. `goal`/`action` are **not provided** by 01's
  event — the adapter leaves them null (or the host pairs the event
  with its own context); this gap is explicit, not an oversight to
  be guessed around.
- `replan_throttled` TraceEvent ← `on_replan_skipped(decision)`
  with `reason == THROTTLED`: `{reason: decision.reason.value,
  decision: False, trigger: decision.detail["throttled_by"],
  last_replan_tick: decision.detail.get("last_replan_tick_id"),
  cooldown_remaining_ms:
  decision.detail.get("cooldown_remaining_ms")}`. When
  `throttled_by == "max_replans_per_tick"`, `budget_name` for 06's
  budget vocabulary is `"replans_per_tick"`.
- `stop_reason`: 01 does not emit `stop_reason`. 06-owned mapping:
  `stats.budget_exhausted → "time_budget"`.
- `attach()` integration (06-owned): the inspector must accept the
  policy object (`replan_policy=...`) to reach `on_replan` /
  `on_replan_skipped`, and subscribe to the planner's
  `on_budget_exhausted`. 01 will not move these hooks.
- Tick fields: `tick_id` is opaque pass-through (int or str, never
  interpreted by 01); 06 derives tick-relative fields from it.
