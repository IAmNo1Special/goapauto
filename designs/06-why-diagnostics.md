# 06 — "Why?" Diagnostics: the Agent Inspector

**Status:** design, revision 2 (Phase 1 — no implementation)
**Library:** goapauto v0.5.0 · Python ≥ 3.12 · stdlib + pydantic in core
**Date:** 2026-09-21 (rev. 2)

Revision 2 answers the red-team review (`designs/reviews/06-why-diagnostics-review.md`,
14 attacks). Headline change: the inspector no longer specifies unilateral
binding schemas *at* the producers. Every subscription, derivation, and
vocabulary below matches exactly what the five producer contracts emit —
01 §9, 02 §9, 03 §9, 04 §9, 05 §11 — cited inline. Where a derivation cannot
be made sound against the 0.5.0 code, it is cut, not repaired.

## 1. Goal and non-goals

### Goal

Build a real inspector — not just a search-tree picture — that answers the
three questions a developer asks when an agent misbehaves:

1. **WHY THIS GOAL** — which goals were candidates, which were already
   satisfied (and thus filtered before arbitration), which strategy decided,
   and, for Jev-backed arbitration, the pick, its confidence, the gate
   decisions on it, latency, backend identity, and which fallback fired.
2. **WHY THIS PLAN** — the goal pursued, search stats (nodes expanded/visited,
   execution time), why the search stopped (found / iteration budget /
   frontier empty / external time budget, derived per §3.8), the chosen plan
   with per-action costs, and any budget or replan events that shaped it.
3. **WHAT CHANGED** since the last tick — `WorldState` diff, sensor updates
   (with producer-reported staleness), goal switches, replan triggers, and
   per-tick sensor health.

The inspector records a timestamped, JSON-serializable `TraceEvent` stream,
offers query APIs (`why_goal()`, `why_plan()`, `what_changed_since()`,
`timeline()`), and exports a Markdown "why" report and JSONL trace. The
search-tree story stays with the existing `SearchTreeVisualizer`
(Mermaid/Graphviz), composed manually in one line — it is not embedded in
the inspector (see §2.6). This differentiator **ingests** the other five:
budget events, sensor staleness, interruptions, gated judgments, and backend
identity land in the trace exactly as their producer contracts emit them
(Section 6, Section 9).

### Non-goals (what we choose NOT to build and why)

- **No live GUI / dashboard.** A GUI is a second product (rendering, event
  loop, deployment). The JSONL trace plus the Markdown report give any
  downstream tool — including a future GUI — everything it needs. Out of
  scope for the library.
- **No network I/O in the inspector.** Trace export is local files only.
  Shipping traces to a server is a deployment concern; the inspector stays a
  pure local observer. (Also: fail-loud local writes, no silent remote
  failures to reason about.)
- **No automatic ticking.** There is no tick concept in goapauto today; the
  agent loop is user code. The inspector defines `mark_tick(state)` and the
  user calls it. Inventing an agent-loop runner to own ticks would be a new
  framework, not a diagnostic.
- **No per-node search recording.** Hooking `on_node_expanded` allocates and
  copies per expanded node — exactly the hot path the zero-cost constraint
  protects. Per-action costs and the search summary are derived at record
  time from `planner.get_search_graph()` inside the plan-hook callbacks
  (§3.1); no graph is retained and no node hook is ever registered.
- **No silent object-identity changes.** Revision 1 wrapped the strategy and
  sensors in transparent decorators installed by `attach()`. Cut: silent
  identity changes break `isinstance` checks, leak through pre-attach
  references, and miss post-attach `add_sensor` calls — the opposite of this
  library's fail-loud contract. All recording is explicit: planner/policy
  hook subscriptions plus user-wired record APIs (§2.8). The one decorator
  the inspector offers (`shim_strategy`) is installed by the user's own code,
  visibly, never by `attach()`.
- **No synthesized producer facts.** The inspector never invents a field a
  producer didn't emit. Every payload in §3.1 cites its source contract; gaps
  are recorded as explicit `None`/absent keys with a documented reason, not
  filled by guessing.
- **No new required dependencies.** stdlib + pydantic only, like the rest
  of core. No pandas, no rich, no plotting.
- **No thread-safety.** Matches the library: one inspector per thread,
  no sharing without external synchronization.

## 2. User-side API

New module `goapauto/utils/inspector.py` (tooling lives in `utils/`, next to
`visualizer.py`). Public symbols re-exported from `goapauto/__init__.py` and
added to `__all__`: `AgentInspector`, `TraceEvent`. (`EventType` is a
`Literal` alias exported for typing convenience.)

### 2.1 Attach / detach

```python
from goapauto import AgentInspector, Planner, GoalArbitrator, SensorManager

planner = Planner(actions_list=[...])
arbitrator = GoalArbitrator(goals=[...], strategy=JevGoalStrategy(client=...))
sensors = SensorManager([JevSensor(...), ...])

inspector = AgentInspector()
inspector.attach(planner, arbitrator=arbitrator, sensor_manager=sensors,
                 replan_policy=policy)
# ... run the agent loop, calling inspector.mark_tick(state) each tick ...
print(inspector.to_markdown())
inspector.to_jsonl("trace.jsonl")
inspector.detach()
```

- `attach(planner, *, arbitrator=None, sensor_manager=None,
  replan_policy=None, max_events=4096, max_report_events=200,
  keep_ticks=2) -> None`
  - Registers recorder callbacks on the planner's hooks:
    `on_plan_found`, `on_search_failed`, `on_action_start`,
    `on_action_complete`, `on_action_failed`, `on_execution_failed`
    (dedup rule, §3.7), `on_budget_exhausted` (01; subscribed only if the
    key exists in `planner.hooks` — absent on 0.5.0 without the budget
    worker, always present with 01 applied), `on_execution_interrupted`
    (03; same conditional rule). `on_execution_complete` is deliberately
    **not** recorded (§3.7). `on_node_expanded` is never touched.
  - If `replan_policy` is given, registers on the policy's **own** hook
    registry (`replan_policy.register_hook`, 01 §9): `on_replan`,
    `on_replan_skipped`. The policy object is the only route to these
    events — 01 will not move them (01 §9). If a policy is in use but not
    passed, replan events are invisible to the trace; the docstring says
    so plainly.
  - `arbitrator` / `sensor_manager` are held by reference for
    `filtered_satisfied` computation (§2.8) and the `diagnostics()` /
    `health()` merges (§3.9). They are never mutated.
  - Attaching twice without detach raises `ValueError` (fail-loud against
    double registration).
- `detach() -> None`
  - Removes hook callbacks by **list replacement**
    (`planner.hooks[event] = [cb for cb in ... if cb is not recorder]`),
    never in-place mutation — safe against an in-flight `_trigger_hook`
    iterating the old list.
  - Sets the recorder's `_attached` flag to `False`. A callback that still
    fires afterwards (in-flight iteration over the pre-detach list) has its
    write **dropped** and counted in `dropped_events` — raising there would
    inject a new exception into the agent's run; dropping + counting keeps
    the trace honest without perturbing the run.
  - Clears the held `arbitrator` / `sensor_manager` / `replan_policy`
    references. Idempotent: detaching twice is a no-op.
  - After detach, the recorded trace and snapshots remain queryable; only
    live recording stops.

Zero-cost when disabled: before `attach()`, the inspector holds no
references and touches nothing — the planner/arbitrator/sensors behave
exactly as if it did not exist.

### 2.2 Tick model

```python
tick_id: int = inspector.mark_tick(state, label="combat")
```

- `mark_tick(state: WorldState, label: str | None = None) -> int`
  - Records a `tick_boundary` event and stores a snapshot
    (`state.get_state()`) for later diffing. Returns the tick id.
  - The `tick_boundary` payload includes per-sensor health from
    `SensorManager.health()` (02 §9.2 — never raises, per contract) when a
    sensor manager is attached: the aggregate staleness answer for the
    tick, no per-call observation needed.
  - Tick ids start at 1 and increase monotonically. Events recorded before
    the first `mark_tick` carry `tick=None` ("unticked").
- `what_changed_since(tick_id: int) -> TickDelta`
  - `TickDelta` fields: `from_tick`, `to_tick`, `state_diff:
    dict[str, tuple[Any, Any]]` (via `WorldState.diff` semantics:
    key → (old, new)), `sensor_updates: list[SensorUpdateInfo]`,
    `goal_switch: tuple[str | None, str | None] | None` (old → new goal
    name, `None` if no switch between the two ticks), `events:
    list[TraceEvent]` (all events in the interval, drawn from the ring
    buffer — may be partial after eviction, see §3.3).
  - Raises `KeyError` if `tick_id` is unknown or its snapshot was evicted
    (§3.3), and `TypeError` if `state` passed to `mark_tick` is not
    a `WorldState`.

The inspector never calls `mark_tick` itself. If the user never ticks,
`what_changed_since` is unusable; `why_goal()` / `why_plan()` still work,
but `PlanDecision.replan` is `None` (unknown, never guessed) and
goal correlation is marked heuristic (§3.2). The docstring says so plainly.

### 2.3 Queries

```python
gd = inspector.why_goal()        # GoalDecision | None
pd = inspector.why_plan()        # PlanDecision | None
tl = inspector.timeline()        # list[TraceEvent], oldest first
tl2 = inspector.timeline(types=["budget_exhausted", "judgment_gated"])
info = inspector.buffer_info()   # BufferInfo(oldest_seq, newest_seq,
                                 #            event_count, dropped_events,
                                 #            orphaned_strategy_records)
```

- `why_goal() -> GoalDecision | None` — the most recent `goal_selected`
  decision, or `None` if no selection has been recorded (e.g. attached
  mid-run before the next arbitration).
- `why_plan() -> PlanDecision | None` — the most recent plan outcome
  (`plan_found` or `plan_failed`), or `None` if no planning has been
  recorded. Carries its `seq`; the report shows the decision's age.
- `timeline(*, types: list[EventType] | None = None, since_tick: int | None
  = None, until_tick: int | None = None) -> list[TraceEvent]` — filtered
  view of the ring buffer, oldest first.
- `buffer_info() -> BufferInfo` — the timeline metadata: retained seq
  range, event count, `dropped_events` (evictions + post-detach drops),
  and `orphaned_strategy_records` (§2.8). Eviction is never silent.

### 2.4 Decision records

```python
@dataclass(frozen=True)
class JevStrategyInfo:
    # Built ONLY from the strategy's JevCallRecord as extended by 04 (§9.2).
    # No field here is synthesized by the inspector.
    pick_label: str | None
    label_matched: bool | None       # None pre-04 (field absent on record)
    fallback_reason: str | None      # "error" | "unknown_label" |
                                     # "low_confidence" | None
    gated: bool | None               # None pre-04
    confidence_absent: bool | None   # None pre-04
    confidence: float | None         # from the "goal" GateDecision, else None
    threshold: float | None          # from the "goal" GateDecision, else None
    gate_decisions: tuple[dict, ...]  # 04's GateDecisions, JSON-safe
    latency_ms: float | None
    error: str | None                # exception class name, None on success
    backend: str | None              # 05's record field; None pre-05
    model: str | None                # 05's record field; None pre-05
    # NOTE: the full probability distribution is NOT available — 04 does not
    # emit it. The trace records the pick and its confidence, not the field.

@dataclass(frozen=True)
class GoalDecision:
    selected: str | None        # goal name (None when strategy returned None)
    candidates: list[str]       # active goals the strategy actually saw
    filtered_satisfied: list[str]  # goals on arbitrator.goals absent from
                                   # candidates (empty when no arbitrator
                                   # attached)
    strategy: str               # class name of the deciding strategy
                                # (inner strategy when shimmed, §2.8)
    jev: JevStrategyInfo | None # set only for Jev-backed strategies with a
                                # correlated telemetry record
    tick: int | None
    seq: int                    # trace sequence number

@dataclass(frozen=True)
class PlanDecision:
    goal: str | None            # correlated goal (see 3.2), None if unknown
    goal_correlated: bool       # True when the goal came from the any-tick
                                # heuristic rather than the same tick
    plan: list[str]             # action names, [] when no plan found
    per_action_costs: list[float | None]
    total_cost: float
    nodes_expanded: int
    nodes_visited: int
    plan_length: int
    execution_time: float       # seconds; PlanStats' own field name
    stop_reason: str            # derived per §3.8: "plan_found" |
                                # "iteration_budget" | "frontier_empty" |
                                # "time_budget"
    budget_exhausted: bool      # 01's raw channel, recorded alongside the
                                # derivation so the derivation is auditable
    search_summary: dict[str, Any]  # expanded_count / visited_count /
                                    # max_depth_reached (source names)
    budget_events: list[TraceEvent]  # budget_exhausted / replan_throttled in
                                     # scope (§3.2)
    replan: bool | None         # True when a plan event already exists this
                                # tick; None when unticked (unknown, §3.2)
    success: bool
    tick: int | None
    seq: int
```

### 2.5 TraceEvent

```python
EventType = Literal[
    "tick_boundary",
    "goal_selected",
    "plan_found",
    "plan_failed",
    "search_error",          # generate_plan raised; no hook fired (§3.8)
    "action_started",
    "action_completed",
    "action_failed",
    "execution_failed",      # defensive: on_execution_failed with no
                             # preceding action_failed (§3.7)
    "execution_interrupted", # 03's signal, verbatim (§6c)
    "replan",                # 01's on_replan decision + outcome (§6a)
    "budget_exhausted",      # 01's signal, via the §9 adapter
    "replan_throttled",      # 01's on_replan_skipped, via the §9 adapter
    "judgment_gated",        # 04's GateDecision, verbatim (§6d)
    "sensor_update",
    "sensor_error",          # sensor raised; recorded, then propagates
]

@dataclass(frozen=True)
class TraceEvent:
    seq: int                    # monotonic, assigned at record time
    tick: int | None            # tick at record time, None if unticked
    type: EventType
    ts: float                   # wall clock, time.time()
    mono: float                 # monotonic, time.monotonic()
    data: dict[str, Any]        # JSON-safe payload (converted at record time)

    def to_dict(self) -> dict[str, Any]: ...
```

All payloads are converted with `json_safe()` at record time so the event is
immutable and JSON-serializable by construction. Per-type payload schemas are
defined in Section 3.1. Dropped from revision 1: `action_interrupted`
(renamed to 03's `execution_interrupted`), `sensor_stale` (folded into
`sensor_update`'s staleness facts), and the never-agreed `record_judgment_gated`
push path.

### 2.6 Exports

```python
md: str = inspector.to_markdown()          # the "why" report (text)
inspector.to_jsonl("trace.jsonl")           # one TraceEvent per line
```

- `to_markdown(max_report_events: int = 200) -> str` — three sections:
  **Why this goal**, **Why this plan**, **What changed** (since the previous
  tick, else since attach). Each section degrades honestly: "no goal
  selection recorded yet" rather than guessing; heuristic correlations are
  labeled as such; evicted history is reported as
  "…N earlier events evicted" (from `buffer_info()`), never silently
  truncated. Event listings are bounded by `max_report_events` with a
  truncation marker naming the full count.
- `to_jsonl(path: str | os.PathLike) -> None` — **overwrites** `path` with
  the current buffer as JSON lines, one object per line:
  `{"seq":…, "tick":…, "type":…, "ts":…, "mono":…, "data":…}` (export
  semantics: a snapshot of the retained buffer; `buffer_info()` reports
  what eviction removed). Write errors propagate (fail-loud); nothing is
  silently dropped.
- Search trees: not embedded. The existing `SearchTreeVisualizer` composes
  manually in one line
  (`planner.register_hook("on_node_expanded", viz.on_node_expanded)`) for
  live Mermaid/Graphviz; the report's plan section uses the compact
  `search_summary`. No `capture_search_tree` flag, no inspector-owned
  delegation.

Export set justification: Markdown is the human "why" report; JSONL is the
machine format every downstream tool (grep, jq, a future GUI) can consume.
No HTML, no live GUI, no network — Section 1.

### 2.7 Full example

```python
inspector = AgentInspector()
inspector.attach(planner, arbitrator=arbitrator, sensor_manager=sensors,
                 replan_policy=policy)

# Explicit wiring — the user composes it; attach() mutates nothing.
strategy = inspector.shim_strategy(
    JevGoalStrategy(client=...,
                    telemetry=lambda r: inspector.note_jev_call("arbiter", r)),
    name="arbiter",
)
arbitrator = GoalArbitrator(goals=[...], strategy=strategy)
danger = JevSensor(mapping={...},
                   telemetry=lambda r: inspector.note_jev_call("danger", r))

state = WorldState(hp=100, enemy_nearby=True)
for i in range(3):  # the user's own loop
    tick = inspector.mark_tick(state, label=f"tick-{i}")
    sensors.update_state(state)
    goal = arbitrator.select_goal(state)   # goal_selected recorded by the shim
    try:
        result = planner.generate_plan(state, goal)
    except Exception as e:
        inspector.record_search_error(e)   # search_error recorded; no hook fired
        raise
    if result.plan:
        state = planner.execute_plan(state, result)  # action_* events

print(inspector.to_markdown())
print(inspector.why_goal())
print(inspector.why_plan().stop_reason)
delta = inspector.what_changed_since(tick - 1)
inspector.to_jsonl("run.jsonl")
inspector.detach()
```

### 2.8 Explicit record APIs (the wrapper replacement)

Revision 1 installed transparent decorators from `attach()`. Every path
below is instead wired explicitly by the user (or by the one shim the
inspector provides). Nothing is installed silently.

- `inspector.note_jev_call(name: str, record: JevCallRecord) -> None`
  — the telemetry ingestion point. The user passes it (usually via a
  lambda) as the `telemetry=` argument of `JevSensor` / `JevGoalStrategy`.
  May also chain a user callback:
  `telemetry=lambda r: (user_cb(r), inspector.note_jev_call("danger", r))`.
  Duck-typed: reads `source`, `latency_ms`, `input_tokens`,
  `output_tokens`, `error`, `stale_cache_hit` (0.5.0 fields) plus any of
  04's / 05's / 02's appended fields when present (`gate_decisions`,
  `gated`, `confidence_absent`, `pick_label`, `label_matched`,
  `fallback_reason`, `backend`, `model`, `staleness`). **Never raises**:
  the 0.5.0 producer swallows telemetry-callback exceptions with
  `logger.exception` (`models/jev.py::_record_telemetry`), so an inspector
  that raised would create silent trace gaps — every merge inside
  `note_jev_call` is guarded, and failures are recorded as a `note` on the
  event, not propagated. This is a transport limitation, documented here,
  not a loudness choice.
  - `source == "sensor"` → a `sensor_update` event (§3.1), with the
    `diagnostics()` merge (§3.9).
  - `source == "strategy"` → stashed as the pending strategy record for
    `name` (consumed by the next `note_goal_selected` for that name, §3.9).
    Overwriting an unconsumed pending record increments
    `orphaned_strategy_records` (visible in `buffer_info()` and the
    report) instead of silently losing it.
- `inspector.shim_strategy(strategy, *, name: str)` — returns an explicit
  recording decorator with exactly one recording method,
  `select(goals, state) -> Goal | None` (the `GoalSelectionStrategy`
  Protocol signature), plus an `.inner` reference and a `strategy_name`
  property reporting `type(inner).__name__`. The user installs it in their
  own construction code (`GoalArbitrator(goals, strategy=shim)`). On
  `select()` it delegates, then calls `note_goal_selected` with the
  candidates the strategy actually saw, the selected goal's label, the
  inner strategy's class name, and the telemetry `name` for jev
  correlation. A raising `select()` records nothing and propagates — a
  selection that never happened must not appear in the trace. Identity
  caveat, stated in the docstring: user code that held the inner strategy
  reference keeps working (`.inner` stays valid); `type()` of the
  installed object is the shim — this is visible composition chosen by
  the user, not silent mutation.
- `inspector.note_goal_selected(*, selected: str | None,
  candidates: list[str], strategy_name: str,
  strategy_record_name: str | None = None) -> None` — the primitive the
  shim calls; also usable directly for custom strategies. `selected` is
  the winning goal's label (`None` when the strategy returned `None`);
  `candidates` are the active goals the strategy saw (post-filter).
  `filtered_satisfied` is computed against `arbitrator.goals` when an
  arbitrator is attached (label rule: `goal.name` or
  `str(goal.target_state)`, mirroring `JevGoalStrategy`'s labeling), else
  `[]`. When `strategy_record_name` names a pending strategy record, it
  is consumed into `JevStrategyInfo`; otherwise `jev` is `None`.
- `inspector.note_sensor_update(*, sensor: str, updates: dict[str, Any],
  duration_ms: float, keys: list[str] | None = None,
  error: str | None = None) -> None` — for non-telemetry sensors (plain
  `Sensor` subclasses, `CachingSensor` skip-path serves). The
  `diagnostics()` merge (§3.9) applies when the sensor is found on the
  attached manager. One line in the user's loop after `update_state`, or
  omitted — the tick-boundary health snapshot still covers aggregate
  staleness.
- `inspector.record_search_error(error: BaseException) -> None` —
  records a `search_error` event. The planner is fail-loud: an exception
  mid-search fires no hook (§3.8), so the user wraps `generate_plan` (see
  §2.7). The original exception is never touched.

## 3. Precise semantics

### 3.1 Event payload schemas

All payloads are plain JSON-safe dicts produced by `json_safe()` at record
time. Each schema cites the contract that fills it.

- `goal_selected`: `{selected, candidates, filtered_satisfied, strategy,
  jev: {…JevStrategyInfo fields…} | None}`. From `note_goal_selected`
  (§2.8); the `jev` block exclusively from 04's strategy-record fields
  (§6d). For `PriorityGoalStrategy`, `jev` is `None` and the Markdown
  report renders candidates in priority order with the winner marked.
- `plan_found`: `{plan, per_action_costs, total_cost, nodes_expanded,
  nodes_visited, plan_length, execution_time, stop_reason: "plan_found",
  budget_exhausted: False,
  search_summary: {expanded_count, visited_count, max_depth_reached}}`.
  From `on_plan_found(plan=plan, stats=stats)`. `plan` is the action-name
  list the hook carries (`_reconstruct_plan` builds name strings).
  `per_action_costs` is derived at record time by a forward walk from the
  search-graph root: the root is the node with `parent is None`; for each
  action name in plan order, take the cost of the edge with
  `from == current_id` and `action == name` (`(from, action)` is unique —
  each node expansion creates one edge per action), then continue from
  `edge["to"]`. On the first missing edge the remaining costs are `None`
  and a `note` field is added — never silently zero. `search_summary`
  keeps `get_search_graph()["metadata"]` key names verbatim.
- `plan_failed`: same shape with `plan: []`, `per_action_costs: []`,
  `stop_reason` derived per §3.8, and `budget_exhausted` from
  `getattr(stats, "budget_exhausted", False)` (01 §9; `False` pre-01).
  From `on_search_failed(stats=stats)` — which 01 also fires on deadline
  expiry, before `on_budget_exhausted` (01 §2.3 migration note), so the
  budget kill is recorded here first and the budget event correlates after.
- `search_error`: `{error, message}` — `type(error).__name__` and
  `str(error)`. From `record_search_error` (§3.8).
- `action_started` / `action_completed`: `{action, goal}` where `goal` is
  the correlated goal name at event time (§3.2; may be `None`).
- `action_failed`: `{action, state, execution_failed}`. From
  `on_action_failed(action=action, state=current_state)`. **No `reason`
  field**: the two call sites fire with identical kwargs, so
  precondition-vs-handler-error is indistinguishable from the hook — the
  revision-1 taxonomy was undeliverable and is cut (§10, attack 10).
  `execution_failed` starts `False` and is set `True` when the paired
  `on_execution_failed` arrives (§3.7). `state` is the JSON-safe snapshot
  of the hook's `current_state`.
- `execution_failed`: `{action, state}`. Defensive only: recorded when
  `on_execution_failed` arrives with no pending `action_failed` for the
  same run. Not observed in 0.5.0 (both hooks always pair); kept so the
  hook is never silently swallowed.
- `execution_interrupted`: `{reason, source, state, executed_actions,
  remaining_actions, step_index, plan_length, state_diff, interrupted_at,
  next_action}`. The first nine are 03's exact kwargs, verbatim (03 §9.1 —
  `state` is 03's defensive deep copy, converted with `json_safe()` at
  record time). `next_action` is **derived** by the inspector as
  `remaining_actions[0]` if non-empty else `None` (03 carries it only as
  an exception property, not hook data — the derivation is marked as such
  in the payload docs). From `on_execution_interrupted`, which 03 fires
  exactly once per interrupted run, fire-then-raise (03 §9.1). Note: 03
  leaves `execute_plan` unchanged — this hook fires on the `PlanExecution`
  path (`begin_execution`/`run`), never from `execute_plan`.
- `replan`: `{reason, detail, plan}`. From 01's
  `on_replan(decision=decision, result=result)` (01 §9):
  `reason = decision.reason.value` (01's wire vocabulary: `no_plan`,
  `goal_satisfied`, `plan_valid`, `plan_invalid`, `goal_changed`,
  `world_changed`, `throttled`, `sensors_stale`), `detail =
  decision.detail` (JSON-safe scalars per 01 §9), `plan` = action-name
  list from `result.plan` (`None` when the replan found nothing). 01's
  `replan()` plans through `generate_plan`, so the outcome also appears
  as `plan_found`/`plan_failed` via the planner hooks (01 §1) — the
  `replan` event carries the *decision*, the plan events carry the
  *outcome*; they link by tick and `seq` order, and the report renders
  them together. This event type is 06-owned vocabulary (01's adapter
  binds only the skipped path).
- `budget_exhausted`: `{budget_name: "time_budget", limit:
  stats.budget_limit, consumed: stats.execution_time, unit: "s",
  phase: "plan", goal: None, action: None}`. The 01→06 adapter, verbatim
  (01 §9). `goal`/`action` are `None` because 01's event does not provide
  them — the gap is explicit, not guessed around. From the planner's
  `on_budget_exhausted(stats=stats)`.
- `replan_throttled`: `{reason, decision: False, trigger,
  last_replan_tick, cooldown_remaining_ms}`. From 01's
  `on_replan_skipped(decision=decision)` (01 §9), adapter verbatim:
  `reason = decision.reason.value`, `trigger =
  decision.detail["throttled_by"]`, `last_replan_tick =
  decision.detail.get("last_replan_tick_id")`, `cooldown_remaining_ms =
  decision.detail.get("cooldown_remaining_ms")`. Recorded for
  `reason == "throttled"` per the adapter; for `reason == "sensors_stale"`
  the same event type is recorded with `{reason: "sensors_stale",
  decision: False, trigger: None, last_replan_tick:
  detail.get("tick_id"), cooldown_remaining_ms: None, sensor_age:
  detail.get("sensor_age")}` — 06-owned extension, same vocabulary,
  fields limited to what 01 provides for that reason.
- `judgment_gated`: `{question, key, confidence, threshold,
  decision: "proceed" | "blocked",
  reason: "passed" | "low_confidence" | "absent_confidence",
  fallback: "drop" | "first" | "priority" | None,
  context, backend, model, latency_ms, error, input_tokens, output_tokens}`.
  04's `GateDecision` fields verbatim (04 §9.1), plus the §9.4 mapping:
  `context` is `"goal"` for the strategy pick (`question == "goal"`),
  else `"sensor:<key>"` (falling back to `"sensor:<question>"` when `key`
  is `None`); `backend`, `model`, `latency_ms`, `error` (and
  `input_tokens`/`output_tokens` when present) come from the enclosing
  record — `None` when the record predates the field. One event per entry
  of `record.gate_decisions` (duck-typed; absent/empty → no events — 04
  emits decisions only when gating was consulted). Revision 1's
  `"flagged"`/`"escalated"` decision tokens are cut: 04 never emits them
  (`"escalated"` names a 04 non-goal); absent-confidence pass-through is
  `decision: "proceed", reason: "absent_confidence"` per 04's invariants.
  04 does not embed the judged value — there is no `value` field; the
  value travels in `sensor_update.updates` and the strategy payload, and
  06 joins on `key` / `pick_label` (04 §9.4).
- `sensor_update` (telemetry-driven): `{sensor, latency_ms, error,
  stale_cache_hit, served_from, input_tokens, output_tokens, backend,
  model, keys, staleness, data_age_seconds, dead_keys, worst_staleness,
  gate_decisions, fully_gated, note}`. From `note_jev_call` with
  `source == "sensor"`:
  - `served_from` is a pure function of the record (sound by
    construction): `"stale_cache"` when `stale_cache_hit` (02 §3.8 edge
    9: failure-path replay), `"live"` when `error is None`, `"none"`
    otherwise (attempt failed, nothing usable served).
  - `staleness` (`{key: "fresh" | "stale"}`), `data_age_seconds`
    (`{key: float}`, seconds, monotonic-derived), `dead_keys`,
    `worst_staleness` — merged from the sensor's duck-typed
    `diagnostics()` (02 §9.4 exact keys) at record time; absent when the
    sensor isn't found on the attached manager or exposes no
    `diagnostics()`. 02's vocabulary is used verbatim — no
    milliseconds, no invented `cache=` strings (02 §9.4: the old
    `data_age_ms` / `cache="hit"|"miss"|"stale"` keys are superseded).
  - `gate_decisions` (04 §9.2), `fully_gated` (04 §5e diagnostics merge).
  - `keys`: the key set of the `diagnostics()` merge when available.
  - `note`: present only when the merge was skipped or failed.
  - Revision 1's `judgment_fresh` (derived from `stats()` call deltas) is
    cut: the derivation labeled stale-cache serves "fresh" (review attack
    3) — provably wrong against `JevSensor._record_telemetry`, which
    increments `calls` on the stale path too.
- `sensor_update` (explicit): `{sensor, keys, updates, duration_ms, error,
  ...diagnostics merge...}` — from `note_sensor_update` (§2.8); no
  `served_from` (the attempt outcome is unknown to the reporter); the
  `diagnostics()` merge applies identically when the sensor is found.
- `sensor_error`: `{sensor, error}` — recorded immediately before the
  original exception propagates (fail-loud preserved).
- `tick_boundary`: `{label, sensor_health}` — `sensor_health` is the
  `SensorManager.health()` list (02 §9.2) as JSON-safe dicts
  (`{name, status, last_success_age, stale_keys, dead_keys, error}`),
  `[]` when no manager is attached.

### 3.2 Goal↔plan correlation

The planner's hooks do not carry the goal, and `generate_plan` is not
wrapped (no planner changes — Section 4). Correlation rule, documented in
the `PlanDecision.goal` docstring:

- The goal for a `plan_found`/`plan_failed` event is the most recent
  `goal_selected` event with `tick` equal to the plan event's tick, if any;
  otherwise the most recent `goal_selected` at any tick — recorded with
  `goal_correlated=True` (heuristic); otherwise `None`
  (`goal_correlated=False`).
- When `None`, the Markdown report says "goal unknown (no arbitration
  recorded — plan() may have been called without the arbitrator)" instead
  of attributing a goal.
- `PlanDecision.replan` is `True` when a plan event already exists for the
  same non-`None` tick, `False` when the tick is set and no earlier plan
  event shares it, and **`None` when the event is unticked** — unknown is
  recorded as unknown, never computed from cross-tick accidents (review
  attack 8).
- `budget_events`: any `budget_exhausted` / `replan_throttled` event whose
  `tick` matches the plan event's tick (or, when unticked, that falls
  between the previous and current plan event by `seq`) is attached to the
  `PlanDecision` and rendered under "Budget events".

Failure modes of the heuristic, stated honestly: in a loop that arbitrates
once and plans repeatedly (or plans without arbitrating), plans after the
first are attributed to a stale goal — the `goal_correlated=True` flag and
the report's "correlated heuristically — call `mark_tick` per loop
iteration for exact attribution" wording exist so the heuristic is never
presented as a fact. The canonical loop (arbitrate → plan → execute per
tick, §2.7) is exact.

### 3.3 Retention bounds

Unbounded buffers are a memory leak in a game loop. All bounds are
constructor/attach parameters with documented defaults:

| Store | Bound | Default | On eviction |
|---|---|---|---|
| Trace events | `max_events` (deque maxlen) | 4096 | oldest events dropped; `seq` keeps increasing; every drop increments `dropped_events` (never silent — §2.3) |
| Tick state snapshots | `keep_ticks` | 2 | oldest snapshot dropped; `what_changed_since` on an evicted id raises `KeyError` naming the id and the oldest retained tick |
| Markdown event listings | `max_report_events` | 200 | listing truncated with "…and N more (timeline() holds the full retained buffer)" |

`max_events=None` is allowed (unbounded) but the docstring carries an
explicit game-loop memory warning. No search-graph retention exists: costs
and summaries are derived at record time (§3.1), so there is nothing to
retain and no lazy-pull-vs-retained-copy contradiction.

`TickDelta.events` is drawn from the same ring buffer under the same
bound: when eviction removed events inside the requested interval, the
report marks the listing partial (via `buffer_info()` seq comparison).
One retention policy, one loudness.

Re-attach after detach with a different `max_events`: the buffer and its
`seq` counter survive (the trace is history); the new bound applies going
forward, evicting oldest-first immediately if the buffer exceeds it, with
each eviction counted in `dropped_events`.

### 3.4 Attaching mid-run

- Events before `attach()` do not exist as far as the inspector is
  concerned. `why_goal()`/`why_plan()` return `None` until the first
  respective event is recorded after attach.
- The first `mark_tick` after attach establishes the baseline snapshot;
  there is nothing to diff against before that.
- Planner hooks that don't exist yet (pre-worker code: `on_budget_exhausted`
  without 01, `on_execution_interrupted` without 03) are skipped by key
  presence, not probed by `register_hook` (which raises `ValueError` on
  unknown names). The attach docstring lists which subscriptions are
  conditional and why.

### 3.5 Serialization: `json_safe()`

New internal helper in `inspector.py`:

- `None`/`str`/`int`/`float`/`bool` pass through; non-finite floats
  (`nan`/`inf`) become their `repr` string (JSON has no NaN).
- `dict` → `{str(k): json_safe(v)}`; `list`/`tuple` → list; `set` /
  `frozenset` → sorted-by-`repr` list (deterministic).
- `WorldState` / `Goal` / pydantic `BaseModel` → `model_dump()` then
  recursed. `Goal.target_state` may contain **callables** (predicates) —
  those become `{"__callable__": qualname}`.
- dataclasses → `dataclasses.asdict()` recursed; `Enum` → `.value`.
- Anything else → `{"__repr__": repr(value)}`. Diagnostics must never
  crash the agent loop on an exotic value; the marker makes the fallback
  visible instead of silent.

`json_safe` is total: it never raises (the `repr` fallback itself is
guarded — if `repr()` raises, the marker becomes
`"__repr__": "<unrepresentable>"`).

Dialect note (review, over-engineering cut 6): `jev._json_safe` renders
callables as a bare `__name__` string and unknown values via `str()`,
while the inspector's `json_safe` uses `{"__callable__": qualname}` and
`{"__repr__": …}` markers. Different jobs — the SDK payload renderer is
built for the TypeSafe wire format, the trace renderer for lossless
debuggability — so the two dialects are kept and documented rather than
unified; a trace value and an SDK payload may legitimately render the
same object differently.

### 3.6 Timing

- `ts` (wall clock) is for humans and JSONL correlation with outside logs.
- `mono` (monotonic) is for durations. `duration_ms` on explicit sensor
  updates is measured by the caller; telemetry `latency_ms` comes from the
  producer's own measurement (monotonic, per 02 §3.2 rule 6).
- Jev latencies inside `JevStrategyInfo` come from the record, not
  re-measured by the inspector.

### 3.7 Action-failure dedup rule

The planner fires `on_action_failed` **and** `on_execution_failed`
back-to-back with identical kwargs at every action failure
(`models/goap_planner.py`, both sync and async paths), then raises. The
inspector records one event, not two:

- `on_action_failed` → the recorder holds the event in a one-slot pending
  buffer instead of finalizing it.
- `on_execution_failed` for the same run → the pending `action_failed` is
  finalized with `execution_failed: True`; no second event.
- Any other hook firing first (next `on_action_start`, `on_action_complete`,
  `on_execution_interrupted`, …) flushes the pending event with
  `execution_failed: False`.
- `on_execution_failed` arriving with no pending `action_failed` → recorded
  as the defensive `execution_failed` event type (not observed in 0.5.0;
  the hook is never silently swallowed).
- `on_execution_complete` → no event, by design: completion is the absence
  of failure/interruption, and the final `action_completed` already marks
  the run's end.

Hooks fire synchronously in a fixed order, so the pending slot always
resolves deterministically.

### 3.8 `stop_reason` derivation (06-owned)

No producer sets `stop_reason` — 01 explicitly declined to extend
`PlanStats` further, choosing `stats.budget_exhausted: bool` as the
structured channel (01 §9). The inspector derives; the derivation is total
and auditable:

- `on_plan_found` → `"plan_found"`.
- `on_search_failed(stats)`:
  - `getattr(stats, "budget_exhausted", False)` is true → `"time_budget"`
    (01's mapping, 01 §9; `getattr` keeps the inspector working on pre-01
    stats where the field doesn't exist — where a time budget cannot occur).
  - elif `stats.nodes_visited >= planner.max_iterations` →
    `"iteration_budget"`. Provable from the 0.5.0 loop
    (`models/goap_planner.py`: `iteration` and `stats.nodes_visited` are
    incremented together at the loop top; the loop exits with
    `iteration == max_iterations` exactly when the budget binds).
  - else → `"frontier_empty"` (the loop exited with iterations to spare).
- The raw `budget_exhausted` bool is recorded alongside in the payload, so
  the derivation can be checked against its input.
- Unknown `stop_reason` strings are rendered verbatim, never rejected.

Failure modes, stated: if a future planner changes the loop accounting
(the `iteration`/`nodes_visited` lockstep), `iteration_budget` vs
`frontier_empty` may mislabel — the payload's `nodes_expanded`,
`nodes_visited`, and the planner's `max_iterations` stay available for
verification. The `time_budget` arm cannot mislabel: it reads 01's own
flag.

Planner raises mid-search: `on_search_failed` fires only on the normal
no-plan path — an exception mid-search fires no hook (fail-loud planner).
The trace would otherwise show the previous tick's plan as current. The
inspector cannot hook an exception it never sees, so the user-side pattern
is explicit (§2.7/§2.8): wrap `generate_plan` and call
`record_search_error(error)`, which records a `search_error` event. The
Markdown "Why this plan" section renders the most recent `search_error`
when its `seq` is newer than the latest plan decision's, with the
decision's tick/`seq` shown so its age is visible. Absence of data is never
presented as data.

### 3.9 Telemetry ingestion rules (`note_jev_call`)

- Duck-typed reads only: `getattr(record, <field>, None)` for every field.
  A 0.5.0 record (six fields) and a 02/04/05-extended record are both
  ingested; absent fields become absent/`None` payload keys, never errors.
- `source` discriminates: `"sensor"` → `sensor_update`; `"strategy"` →
  pending strategy slot for `name` (§2.8). Unknown `source` values are
  recorded as a `sensor_update` with a `note` (fail-loud about the unknown,
  not silent).
- The `diagnostics()` merge: the inspector finds `name` among the attached
  `sensor_manager.sensors` (02 §2.4 `Sensor.name`, else
  `type(sensor).__name__`; first match wins) and merges the duck-typed
  `diagnostics()` mapping — 02's exact keys (`data_age_seconds`,
  `staleness`, `dead_keys`, `worst_staleness`, 02 §9.4) plus 04's
  (`gating_enabled`, `effective_thresholds`, `gated_questions_last_call`,
  `fully_gated_last_call`, `gated_total`, 04 §5e) — into the event at
  record time. Merge timing is sound: the producer fires telemetry
  synchronously after updating its cache/report state (`_judge` sets
  `_cached`/`_last_success` before `_record_telemetry`), so
  `diagnostics()` reflects the just-completed call.
- The merge is guarded: any exception becomes a `note` on the event
  (never raised — §2.8 transport limitation). Sensor not found, or no
  `diagnostics()` method → merge skipped, `note` records which.
- What telemetry can never show (documented gaps, not bugs): the
  resense-skipped path records no telemetry in 0.5.0 (no re-sense
  attempted — nothing to report), so skip-path serves appear only via
  explicit `note_sensor_update` or the tick-boundary health snapshot.
  Deriving them from age growth between ticks is refused: "no telemetry +
  age grew" cannot distinguish "sense() skipped" from "sense() not
  called".

## 4. Changes to existing code

**None.** Revision 2 requires zero changes to existing code:

- All planner hooks consumed (`on_plan_found`, `on_search_failed`,
  `on_action_start`, `on_action_complete`, `on_action_failed`,
  `on_execution_failed`) exist in 0.5.0 with the exact kwargs used here
  (verified against `models/goap_planner.py`).
- `on_budget_exhausted` is added by 01 (§9, registered in
  `Planner.__init__`); `on_execution_interrupted` by 03 (§9.1). Both are
  subscribed conditionally by key presence — no 06-side change needed
  when the workers land.
- `ReplanPolicy.register_hook` and its `on_replan` / `on_replan_skipped`
  events are 01-owned (01 §9); the inspector only subscribes.
- Telemetry callbacks, `diagnostics()`, `SensorManager.health()`,
  `last_report()` are producer-owned surfaces (02 §9, 04 §5e/§9); the
  inspector only reads.
- The revision-1 additives are **cut**: `JevGoalStrategy.last_decision`
  (superseded — 04's strategy-record fields carry the same facts, §6d)
  and `PlanStats.stop_reason` (superseded — 01 declined it; §3.8 derives
  instead). No producer doc needs to change for 06 (see §10).

## 5. Failure-mode table

| Failure mode | Handling |
|---|---|
| Recorder callback itself raises (bug in inspector code) | Propagates to the caller — consistent with the planner's fail-loud hook philosophy. Inspector code paths are kept trivially small to make this unlikely; the 100%-coverage suite exercises every callback. |
| `note_jev_call` internals raise (merge failure, bad record) | Never propagates: guarded, recorded as `note` on the event. The 0.5.0 producer swallows telemetry-callback exceptions, so raising would create silent trace gaps (§2.8). |
| Serialization of an exotic state value | Never raises: `json_safe()` falls back to `{"__repr__": …}` (§3.5). |
| `what_changed_since` with unknown tick id | `KeyError` naming the id (fail-loud; silent empty diffs would lie). |
| `what_changed_since` with evicted tick id | `KeyError` naming the id and the oldest retained tick id. |
| `mark_tick` with non-`WorldState` | `TypeError`. |
| `attach()` called twice without `detach()` | `ValueError` (double hook registration would duplicate every event). |
| `detach()` without `attach()` | No-op (idempotent). |
| Detach during in-flight execution | Hook callbacks removed by list replacement; in-flight iterations over the old list hit the `_attached` flag — writes dropped, counted in `dropped_events`. The agent run is never perturbed. |
| `to_jsonl` to an unwritable path | `OSError` propagates; the in-memory buffer is unaffected. |
| Event raised from inside a sensor's `sense()` | `sensor_error` recorded, then the original exception propagates unchanged (fail-loud preserved). |
| Strategy raises inside the shim | No `goal_selected` recorded; the exception propagates unchanged. A selection that never happened must not appear in the trace. |
| Strategy telemetry with no following selection | Pending slot overwritten → `orphaned_strategy_records` incremented; surfaced in `buffer_info()` and the report. Never silently lost, never misattributed. |
| Non-finite floats in state | Serialized as their `repr` string (§3.5); JSON stays valid. |
| `why_goal()` / `why_plan()` with nothing recorded | Return `None` — documented, not an error. Callers check. |
| Tick snapshots vs. large states | Snapshots are `get_state()` dicts (shallow per value); values are the user's own objects, shared by reference — documented as not deep-copied for cost reasons, with the caveat that mutating a state *value in place* after `mark_tick` corrupts the diff. (WorldState values must be hashable; in-place mutation of a hashable is already a planner hazard.) |
| Replan policy in use but not passed to `attach()` | Replan events invisible to the trace. Documented in the `attach()` docstring; the inspector cannot discover the policy object. |
| `max_events` eviction | Counted in `dropped_events`; reported in `buffer_info()`, the Markdown report, and detectable via `timeline()[0].seq > 1`. Never silent. |
| Markdown report size | Bounded by `max_report_events` (default 200) with a truncation marker. |
| Enabled-mode payload cost | Every event runs `json_safe` over its payload at record time. No per-node hook exists, but a chatty sensor's full `sense()` dict is still copied per event — the docstring carries the cost warning the zero-cost framing would otherwise obscure. |

## 6. Interaction notes — consuming the other five differentiators

The inspector is the consumer; each worker owns its producer side. Field
names here are binding **because the producer contracts bind them** —
every mapping below cites the exact contract section.

### (a) Runtime / replan budgets → `budget_exhausted`, `replan_throttled`, `replan`

- 01 fires `on_budget_exhausted(stats=stats)` — kwargs exactly
  `{"stats": PlanStats}` — immediately after `on_search_failed`, iff the
  deadline stopped the search (01 §9). The inspector subscribes on the
  planner (key presence-checked).
- 01's `ReplanPolicy` owns its hook registry (01 §9): the inspector
  subscribes via the `replan_policy=` attach parameter to
  `on_replan(decision=decision, result=result)` and
  `on_replan_skipped(decision=decision)`. 01 will not move these hooks.
- The 01→06 adapter is 06-owned code; the mapping is 01 §9 verbatim
  (mirrored in §9.1):
  - `budget_exhausted` ← `on_budget_exhausted(stats)`:
    `{budget_name: "time_budget", limit: stats.budget_limit, consumed:
    stats.execution_time, unit: "s", phase: "plan", goal: None,
    action: None}`. `goal`/`action` are `None` — 01's event does not
    provide them; the gap is explicit.
  - `replan_throttled` ← `on_replan_skipped(decision)` with
    `reason == THROTTLED`: `{reason: decision.reason.value, decision:
    False, trigger: decision.detail["throttled_by"], last_replan_tick:
    decision.detail.get("last_replan_tick_id"), cooldown_remaining_ms:
    decision.detail.get("cooldown_remaining_ms")}`.
    (`decision` is the bool `False` per the adapter — not a string.)
  - `stop_reason`: 01 does not emit it. 06-owned mapping:
    `stats.budget_exhausted → "time_budget"` (§3.8).
  - Ticks: 01's `tick_id` is opaque pass-through; 06 derives tick-relative
    correlation from its own tick model (§3.2).
- `replan` ← `on_replan(decision, result)`: `{reason:
  decision.reason.value, detail: decision.detail, plan: [...] | None}`
  (06-owned vocabulary; the decision event — the outcome arrives via the
  planner hooks since 01's `replan()` plans through `generate_plan`,
  01 §1).
- How the inspector consumes them: any `budget_exhausted` /
  `replan_throttled` event whose `tick` matches the plan event's tick (or,
  when unticked, falls between the previous and current plan event by
  `seq`) is attached to `PlanDecision.budget_events` and rendered in the
  Markdown "Why this plan" section under "Budget events". The inspector
  never synthesizes these events itself — if the budget worker is absent,
  the section honestly reports "no budget events recorded".
- `ReplanReason` wire values consumed: `no_plan`, `goal_satisfied`,
  `plan_valid`, `plan_invalid`, `goal_changed`, `world_changed`,
  `throttled`, `sensors_stale` (01 §9; `str` Enum, `.value` used).

### (b) Sensor caching / staleness → `sensor_update`, `tick_boundary`

- 02's binding vocabulary (02 §9.1): staleness states are exactly
  `"fresh"` / `"stale"` / `"dead"`; per-key ages are **seconds** (float,
  monotonic); `latency_ms` keeps its name and unit. The inspector uses
  this vocabulary verbatim — revision 1's `data_age_ms`,
  `judgment_fresh`, and `cache="hit"|"miss"|"stale"` are superseded per
  02 §9.4 and cut.
- Per-call ingestion is the telemetry record (`note_jev_call`, §2.8/§3.9),
  not `stats()`-delta derivation (cut — provably wrong, §3.1).
  The record's `stale_cache_hit` + `error` give the attempt outcome
  (`served_from`); 02's per-key bands come from the `diagnostics()` merge
  (02 §9.4 exact keys). 02's moments table (§9.5) is consumed as:
  - live re-sense served → `sensor_update` with `served_from: "live"`,
    per-key `staleness: "fresh"`;
  - failure-path cache replay → `sensor_update` with
    `served_from: "stale_cache"`, `error` set, per-key ages/bands from
    `diagnostics()`;
  - STALE-band policy serve / skipped re-sense → no telemetry exists in
    0.5.0; visible via the tick-boundary `sensor_health` snapshot and via
    explicit `note_sensor_update`. Not derived (§3.9).
  - 02's `stale_cache_hits` semantics are respected, not re-derived: the
    counter counts failure-path replays only (02 §3.8 edge 9) — the
    inspector never diffs it.
- Per-tick aggregate: `tick_boundary.sensor_health` from
  `SensorManager.health()` (02 §9.2 — `{name, status, last_success_age,
  stale_keys, dead_keys, error}`, never raises).
- What 06 must not assume (02 §9.5): ages are build-time-frozen
  (`age_seconds` recomputed per decision, never compared across reports);
  wall-clock correlation of monotonic `as_of` values is 06's choice —
  06 correlates by `seq`/tick, not by clock.

### (c) Action interruption → `execution_interrupted`

- Adopted verbatim from 03 §9.1: hook **`on_execution_interrupted`**,
  event **`execution_interrupted`**, the exact nine kwargs (`reason`,
  `source`, `state`, `executed_actions`, `remaining_actions`,
  `step_index`, `plan_length`, `state_diff`, `interrupted_at`) — no more,
  no fewer. `source` is 03's closed `InterruptSource` literal
  (`"manual"`, `"sensor"`, `"watchdog"`, `"budget"`, `"arbitration"`,
  `"confidence"`, `"cancellation"`).
- `step_index` / `plan_length` are **carried data** (03 §9.1) — used as
  given, never re-derived. `next_action` is derived by the inspector from
  `remaining_actions` (03 carries it only as an exception property) and
  marked derived.
- 03 guarantees exactly-once firing per interrupted run, fire-then-raise,
  never alongside `on_execution_complete`/`on_execution_failed` for the
  same run (03 §9.1). The inspector relies on the guarantee — no dedup
  needed, unlike §3.7.
- 03's deliberate choice is honored: interruption is **not** routed through
  `on_execution_failed` (03 §9.1); the inspector keeps the event types
  separate. Revision 1's open questions about hook-vs-exception were
  already decided by 03 — they are removed.
- Scope note: 03 leaves `execute_plan` unchanged — this hook fires on the
  `PlanExecution` path (`begin_execution`/`run`), never from
  `execute_plan` (03 §9.2). The inspector subscribes on the planner; which
  execution path the user drives decides whether the event can occur.

### (d) Confidence gating → `judgment_gated`

- 04's architecture is pull, and 06 pulls (04 §9.4): **no
  `record_judgment_gated` push API exists** — revision 1's push call is
  cut. The inspector ingests `record.gate_decisions` (duck-typed) from the
  telemetry stream (§2.8) and translates each `GateDecision` into a
  `judgment_gated` event per 04 §9.1/§9.4.
- Field vocabulary is 04's verbatim: `question`, `key`, `confidence`,
  `threshold`, `decision` (`"proceed"` | `"blocked"`), `reason`
  (`"passed"` | `"low_confidence"` | `"absent_confidence"`), `fallback`
  (`"drop"` | `"first"` | `"priority"` | `None`), with 04's invariants
  (`decision == "blocked"` iff `reason == "low_confidence"`, etc.).
  `context` ← `"sensor:<key>"` / `"goal"` (04 §9.4); `backend`, `model`,
  `latency_ms`, `error` ← the enclosing record.
- Every gated judgment is an event — including `"proceed"`. A gate that
  only logs blocks is a lie by omission; the interesting debugging question
  is "why did it act on low confidence?" (04's test plan pins proceed
  visibility; 04 §6).
- 04 does not embed the judged value (04 §9.5) — no `value` field; 06 joins
  on `key` / `pick_label` (04 §9.4). `None` confidence is pass-through +
  flagged, never coerced (04 §3.2/§5d): `decision: "proceed", reason:
  "absent_confidence"`.
- `JevStats.gated` is a calibration counter (blocks only, 04 §9.3), not an
  event log — the event log is `gate_decisions`. The inspector never diffs
  it.
- Strategy pick detail comes from 04's record fields (`pick_label`,
  `label_matched`, `fallback_reason`, `gated`, `confidence_absent`,
  04 §9.2) — revision 1's `JevGoalStrategy.last_decision` additive is cut
  as overlapping (§4). The raw unknown-label text is not emitted by 04;
  the trace records `fallback_reason: "unknown_label"` with
  `label_matched: False` and documents the gap (§6d note in §3.1).
- `JevSensor.diagnostics()` (04 §5e) merges into `sensor_update` via the
  §3.9 merge (`gating_enabled`, `effective_thresholds`,
  `gated_questions_last_call`, `fully_gated_last_call`, `gated_total`).
  `fully_gated=True` on a fresh record means "fresh but unconfident
  perception" — rendered as such, never as dead cache (04 §9.4).

### (e) Provider-independent judgment interface → `backend` on judgment events

- Canonical names are 05's (05 §11): the TypeSafe backend is `"jev"`.
  The inspector **never synthesizes** a backend name: `backend` on an event
  is the record's `backend` when present (05), else `None` with the
  documented note "backend identity unavailable pre-05". The `sensor`
  field (02's name/class name) remains the pre-05 identity.
- 05's rule is followed literally: `backend_name()` for sensor-level
  identity, `JudgmentResponse.backend` for call-level identity, when those
  accessors exist (05 §11).
- Consumed from 05 §11: `JudgmentResponse.backend`, `.model`, `.latency_ms`,
  `.usage` (`.input_tokens`/`.output_tokens`); `JudgmentCallRecord`'s
  `source`, `latency_ms`, `input_tokens`, `output_tokens`, `error`
  (cause-chain-resolved class name), `stale_cache_hit`, `backend`;
  `JudgmentError.backend` / `.retryable` for "why did judgment fail"
  traces. Never assumed: cross-backend confidence calibration, in-flight
  `judge()` observability, or any non-canonical backend spelling.
- 05's translation guarantees bound what the trace can claim: nothing
  named `TypeSafeError` crosses `JevJudge.judge` outward (05 §11) — the
  trace's `error` names are the translated surface, and the frozen
  `JevSensor`/`JevGoalStrategy` keep their own handling untouched.

## 7. Test plan sketch (100%-coverage suite)

Tests live in `tests/goapauto/utils/test_inspector.py` (+ fixtures).
Coverage is `fail_under=100` with no `pragma: no cover` in source — every
branch below must be hit, including every `json_safe` fallback, every
`None`-return path, and every guarded merge in `note_jev_call`.

- **Event recording:** each subscribed planner hook (`on_plan_found`,
  `on_search_failed`, `on_action_start/complete/failed`,
  `on_execution_failed`, `on_budget_exhausted` via synthetic stats,
  `on_execution_interrupted` with 03's exact nine kwargs) produces the
  correctly-typed event with the documented payload; async paths record
  identically.
- **Action-failure dedup:** paired hooks → one `action_failed` with
  `execution_failed: True`; lone `on_execution_failed` → defensive
  `execution_failed` event; `on_execution_complete` → no event.
- **Goal decisions:** shimmed `PriorityGoalStrategy` records candidates,
  `filtered_satisfied`, `strategy="PriorityGoalStrategy"`, `jev=None`.
  Shimmed `JevGoalStrategy` with `FakeTypeSafeClient`: telemetry record
  correlated into `jev` (pick_label, confidence from the "goal" gate
  decision when gating, latency, backend None pre-05); fallback paths
  (error → `fallback_reason="error"`; unknown label →
  `fallback_reason="unknown_label"`, `label_matched=False`); strategy
  raising → nothing recorded, propagates; direct (unshimmed) select with
  telemetry → orphan counter increments.
- **Plan decisions:** `why_plan()` after success and failure;
  `stop_reason` derivation — `max_iterations=1` → `"iteration_budget"`,
  open search → `"frontier_empty"`, synthetic `budget_exhausted=True`
  stats → `"time_budget"` with the raw flag recorded; `per_action_costs`
  forward-walk from the graph root incl. the missing-edge `note` path
  (hostile graph fixture); `replan` True/False/None incl. unticked;
  `goal_correlated` flag incl. the any-tick fallback and the `None` case.
- **Search errors:** `record_search_error` → `search_error` event; report
  renders it newer than the last plan decision with the decision's age.
- **Ticks and diffs:** `mark_tick`/`what_changed_since` with changed,
  added, removed keys; `goal_switch` detection; `KeyError` on unknown and
  evicted ids (`keep_ticks=2` forcing eviction); `TypeError` on
  non-`WorldState`; `tick_boundary.sensor_health` from a stub manager.
- **Retention:** `max_events` eviction increments `dropped_events`;
  `buffer_info()` reflects it; Markdown renders the eviction line;
  `max_report_events` truncation marker; re-attach with smaller
  `max_events` evicts-to-fit and counts.
- **Mid-run attach:** attach after a plan → `why_plan()` is `None` until
  the next plan; double `attach()` raises `ValueError`; double `detach()`
  is a no-op; detach mid-`execute_plan` from inside a hook callback drops
  post-detach writes and counts them.
- **Exports:** `to_jsonl` round-trips every event type incl. `mono`
  (parse each line back, compare `to_dict()`); overwrite (not append)
  semantics; `to_markdown` contains the three section headers, the honest
  "not recorded" fallbacks, the heuristic-correlation wording, and the
  eviction line; `to_jsonl` to an unwritable path raises.
- **Serialization:** `json_safe` over callables in `target_state`,
  non-finite floats, sets, tuples, nested dataclasses, pydantic models,
  objects with raising `repr`.
- **Failure modes:** sensor raising inside `sense()` → `sensor_error` then
  propagates; `note_jev_call` with a raising `diagnostics()` → event kept
  with `note`, nothing propagates; 0.5.0 six-field records and fully
  extended records both ingest.
- **Zero-cost:** with no inspector attached, `planner.hooks` lists are
  empty and a planning run's stats are byte-identical to a run without the
  inspector ever existing (assert on `PlanStats` equality).
- **Cross-worker contract tests:** synthetic payloads built from the exact
  producer contracts — 01's adapter table (stats with `budget_exhausted` /
  `budget_limit`; `ReplanDecision` with `reason.value` and `detail` keys),
  03's nine kwargs, 04's `GateDecision` vocabulary incl. the invariants,
  02's `diagnostics()` exact keys, 05's `"jev"` backend — assert the
  inspector ingests, correlates (`budget_events`), and renders each, so
  the five workers have an executable contract to code against.

## 8. Open questions (recorded, not guessed)

1. **TraceEvent: dataclass or pydantic?** The sketch uses a frozen
   dataclass for zero-validation-overhead recording on the hot path.
   The codebase leans pydantic for domain models. If the team prefers
   pydantic consistency, `TraceEvent` becomes a frozen
   (`model_config = ConfigDict(frozen=True)`) `BaseModel` — validation
   cost per event is the trade-off to decide.
2. **Who owns ticks long-term?** This design puts `mark_tick` on the
   user. If a future agent-loop helper arrives, tick ownership should
   move there and the inspector should accept ticks from it — the
   `tick: int | None` field already supports externally-assigned ids.
3. **Default `max_events` / `max_report_events`.** 4096/200 are judgment
   calls. Soulscape's per-frame budget may want smaller; long debugging
   sessions may want larger. They're parameters — the question is only
   what ships as default.
4. **Should the inspector also record `continue_plan` specially?** It
   fires `on_plan_found` like a fresh plan. A continued plan is arguably
   a replan — the `replan` heuristic (two plan events, one tick) already
   flags it, but an explicit `continued: bool` on `PlanDecision` may be
   clearer. Deferred to implementation review.
5. **`note_jev_call` chaining ergonomics.** The lambda-wire pattern works
   but is slightly awkward for users who already pass their own telemetry
   callback. If it proves annoying in practice, a tiny
   `inspector.chain_telemetry(*callbacks)` helper composes them —
   additive, no design change needed.

(Resolved and removed from this list: `action_failed` reason taxonomy —
cut as undeliverable, §3.1; interruption signal shape — 03 decided, §6c;
`record_judgment_gated` on detached inspectors — push API cut, §6d;
Mermaid truncation — visualizer not embedded, §2.6.)

## 9. Producer contract (06-owned — binding)

This section is what other docs and hosts may build against. 01 §9
references the 01→06 adapter: the mapping below mirrors 01 §9 verbatim,
and 06's implementation must match it literally.

### 9.1 The 01→06 adapter (06-owned code; 01 emits, 06 maps)

| Producer event | Trace event | Exact mapping |
|---|---|---|
| `on_budget_exhausted(stats)` (planner) | `budget_exhausted` | `{budget_name: "time_budget", limit: stats.budget_limit, consumed: stats.execution_time, unit: "s", phase: "plan", goal: None, action: None}` |
| `on_replan_skipped(decision)`, `reason == THROTTLED` (policy) | `replan_throttled` | `{reason: decision.reason.value, decision: False, trigger: decision.detail["throttled_by"], last_replan_tick: decision.detail.get("last_replan_tick_id"), cooldown_remaining_ms: decision.detail.get("cooldown_remaining_ms")}` |
| `on_replan_skipped(decision)`, `reason == SENSORS_STALE` (policy) | `replan_throttled` | `{reason: "sensors_stale", decision: False, trigger: None, last_replan_tick: decision.detail.get("tick_id"), cooldown_remaining_ms: None, sensor_age: decision.detail.get("sensor_age")}` (06-owned extension, §3.1) |
| `on_replan(decision, result)` (policy) | `replan` | `{reason: decision.reason.value, detail: decision.detail, plan: [names] \| None}` (06-owned vocabulary, §3.1) |
| `stats.budget_exhausted` | `stop_reason` | `True → "time_budget"` (06-owned derivation, §3.8) |

Neither side synthesizes: 01 emits exactly its §9 shapes; 06 maps exactly
the table above. `goal`/`action` on `budget_exhausted` are `None` by
contract — hosts pair the event with their own context.

### 9.2 Event vocabulary owned by 06

`EventType` (§2.5) and every payload schema in §3.1 are 06-owned. 02
reserves the trace event vocabulary to 06 (02 §9): 02/04/05 emit
record fields and mappings, never event types. Producer-owned vocabularies
06 carries verbatim (never redefined): 01's `ReplanReason` wire values,
02's `"fresh"`/`"stale"`/`"dead"` bands, 03's `InterruptSource` literal,
04's `GateDecision` fields and invariants, 05's canonical backend strings.

### 9.3 Attach wiring contract (06-owned)

- `attach()` accepts `replan_policy=` to reach 01's policy-owned hooks;
  01 will not move them (01 §9).
- `attach()` subscribes to the planner's `on_budget_exhausted` (01) and
  `on_execution_interrupted` (03) by key presence — present post-worker,
  skipped pre-worker, never probed via `register_hook`.
- Telemetry ingestion is user-wired (`note_jev_call` as the sensors' /
  strategy's `telemetry=` callback); goal selection is user-wired
  (`shim_strategy` installed by user code, or `note_goal_selected`
  directly). `attach()` installs no wrappers and mutates no user objects.

### 9.4 Host guarantees

- Every recorded event is JSON-safe at record time (`json_safe`, §3.5).
- `seq` is monotonic across the inspector's lifetime, including across
  detach/re-attach. Eviction is counted (`dropped_events`) and reported,
  never silent.
- Heuristic outputs (`goal_correlated`, `replan=None` when unticked) are
  labeled in both the data and the report — never presented as facts.
- Unknown producer vocabulary is rendered verbatim, never rejected; the
  inspector does not break when a worker extends its vocabulary.

## 10. Adversarial review (red-team, 14 attacks — dispositioned)

Revision 1's review (`designs/reviews/06-why-diagnostics-review.md`).
Disposition per attack:

1. **Budget/replan events structurally unreachable** — **RESOLVED.** (a)
   `attach()` now subscribes to `on_budget_exhausted` (key-presence
   checked) and (b) accepts `replan_policy=` to reach the policy's own
   registry — the wiring 01 §9 mandates ("01 will not move these hooks").
   (c) Field schemas replaced by 01's adapter table verbatim (§9.1):
   `(stats=stats)` kwargs, `unit: "s"`, `phase: "plan"`,
   `decision: False` (bool, not string), `goal`/`action` explicit `None`.
2. **`stop_reason` None on budget exhaustion; real flag dropped** —
   **RESOLVED** by derivation, not by producer change: §3.8 derives
   `stop_reason` from `stats` (`budget_exhausted → "time_budget"`;
   `nodes_visited >= max_iterations → "iteration_budget"`, provable from
   the 0.5.0 loop; else `"frontier_empty"`), and the raw
   `budget_exhausted` bool is recorded alongside so the derivation is
   auditable. The `PlanStats.stop_reason` additive is cut — 01 declined
   it, and 06 does not ask again.
3. **Staleness derivation provably wrong** — **RESOLVED** by deletion and
   rebuild: the `stats()`-delta `judgment_fresh` rule is cut (it labeled
   stale-cache serves "fresh"); `data_age_ms` / `cache=` keys cut as
   superseded by 02 §9.4. All staleness facts now come from the telemetry
   record (`stale_cache_hit`, `error` → `served_from`) and the
   `diagnostics()` merge (02's exact keys, seconds, `"fresh"`/`"stale"`/
   `"dead"` vocabulary). The telemetry callback — the producer-built
   channel the review asked why we bypassed — is now the ingestion path
   (`note_jev_call`).
4. **Interruption: wrong hook/event/fields** — **RESOLVED.** Adopted 03
   verbatim: `on_execution_interrupted`, event `execution_interrupted`,
   the exact nine kwargs; `step_index`/`plan_length` used as carried data;
   `next_action` derived and marked derived. Revision 1's open questions
   7.5/7.6 removed — 03 decided them.
5. **`judgment_gated` push API contradicts 04** — **RESOLVED.**
   `record_judgment_gated` cut entirely. 06 ingests `record.gate_decisions`
   (duck-typed) per 04 §9.4; the event vocabulary is 04's `GateDecision`
   verbatim; `"flagged"`/`"escalated"` cut (`"escalated"` names a 04
   non-goal); no `value` field (04 §9.5 — 04 doesn't emit it); no
   inspector reference held by producers; no detached-inspector
   `ValueError` (the situation cannot arise).
6. **Backend naming contradicts 05** — **RESOLVED.** 05's canonical
   `"jev"` adopted; the `"typesafe"` hardcode and the module-qualified
   duck-typing cut. The inspector never synthesizes a backend name:
   record's `backend` when present, else `None` with a documented note
   (05 §11: `backend_name()` / `JudgmentResponse.backend` when those
   accessors exist).
7. **Transparent wrappers break identity** — **RESOLVED** by cut.
   `attach()` installs nothing and mutates nothing. Recording is explicit:
   hook subscriptions + user-wired `note_jev_call` / `note_sensor_update`
   / `record_search_error` + the user-installed `shim_strategy` (one
   explicit `select` method, `.inner` reference, visible composition —
   the review's "why is `mark_tick` user-called but recording done by
   invisible mutation?" now has an answer: everything is user-called).
8. **`mark_tick` forgotten → heuristics lie** — **RESOLVED.**
   `replan` is `None` (not `False`) when unticked; goal correlation falls
   back with `goal_correlated=True` and the report labels it heuristic
   ("call `mark_tick` per loop iteration for exact attribution").
9. **Two hooks with no event type; double-counted failures** —
   **RESOLVED** by the §3.7 dedup rule, stated explicitly:
   `on_execution_complete` → no event (by design); `on_action_failed`
   held in a one-slot pending buffer and finalized with
   `execution_failed: True` when the paired `on_execution_failed`
   arrives; lone `on_execution_failed` → defensive `execution_failed`
   event (never silently swallowed).
10. **`action_failed` reason taxonomy undeliverable** — **RESOLVED** by
    cut. Both call sites fire identical kwargs; the precondition/handler
    distinction is unimplementable from the hook. No `reason` field until
    a planner change carries one (no such change requested — §4: zero
    changes to existing code).
11. **Planner raises mid-search → stale `why_plan()`** — **RESOLVED.**
    `record_search_error(error)` explicit API + user-side wrap pattern
    (§2.7); the report renders the newest `search_error` ahead of any
    older plan decision, with the decision's tick/`seq` shown. Absence of
    data is never presented as data.
12. **Detach during in-flight execution unspecified** — **RESOLVED.**
    Hook removal by list replacement (never in-place mutation during
    iteration); recorder checks `_attached` on every write — post-detach
    writes are dropped and counted in `dropped_events` (raising would
    inject exceptions into the agent's run); no wrappers remain, so no
    sensor-list rebinding question. Test: detach mid-`execute_plan` from
    inside a hook callback.
13. **Silent eviction, inconsistent loudness, unbounded Markdown** —
    **RESOLVED.** `dropped_events` monotonic counter (evictions +
    post-detach drops); `buffer_info()` exposes it; the Markdown report
    renders "…N earlier events evicted"; event listings bounded by
    `max_report_events` (default 200) with a truncation marker;
    `TickDelta.events` partiality marked via `buffer_info()` seq
    comparison — one retention policy, one loudness.
14. **`last_decision` staleness; unknown-label has no schema field** —
    **RESOLVED** by cut. `JevGoalStrategy.last_decision` is not specified
    (04's strategy-record fields — `pick_label`, `label_matched`,
    `fallback_reason`, `gated`, `confidence_absent` — carry the same
    facts; two overlapping mechanisms are not specified). The full
    probability distribution is not promised (04 doesn't emit it). The raw
    unknown-label text is not emitted by 04 either — the trace records
    `fallback_reason: "unknown_label"` and documents the gap rather than
    inventing a field.

**Change requests to producer docs: none.** Every resolution above is by
adoption (01's adapter, 02's vocabulary, 03's hook, 04's record fields,
05's backend names), by 06-owned derivation with stated failure modes
(§3.8), or by cut. 06 requires zero producer changes (§4). Rejected
alternatives, recorded: asking 01 to set `stop_reason` (01 already
reasoned against extending `PlanStats`; derivation chosen instead);
a planner `reason=` kwarg on `on_action_failed` (breaks existing
callbacks; distinction dropped instead); deriving skip-path sensor serves
from inter-tick age growth (cannot distinguish "skipped" from "not
called" — refused as unsound).

**Review's over-engineering cuts — adopted:** transparent wrappers (cut,
§2.8); speculative event types (`sensor_stale` folded into
`sensor_update`; `budget_exhausted`/`replan_throttled` kept — now bound by
01's signed adapter; `action_interrupted` renamed to 03's
`execution_interrupted`; `judgment_gated` kept — now 04's verbatim
vocabulary); embedded `SearchTreeVisualizer` (cut — manual one-line
composition documented in §2.6; per-action costs and summaries derived at
record time, no graph retained, no `capture_search_tree`); `keep_ticks=64`
→ 2; `record_judgment_gated` (cut, attack 5); `json_safe` vs
`jev._json_safe` (kept as documented dialects, §3.5).

**Review's minor inconsistencies — all fixed:** `to_jsonl` includes `mono`
and is overwrite (export) semantics, not append; `search_summary` keeps
source key names (`expanded_count`, `visited_count`,
`max_depth_reached`); plan payload uses `execution_time` (PlanStats'
name); `per_action_costs` specifies the forward-from-root edge walk
(`(from, action)` uniqueness); re-attach with different `max_events`
specified (§3.3); shim reports the inner strategy's class name (§2.8).
