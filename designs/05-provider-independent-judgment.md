# 05 — Provider-Independent Judgment Interface

**Status:** design (Phase 1 — no implementation)
**Library:** goapauto v0.5.0 · Python ≥ 3.12 · stdlib + pydantic in core
**Date:** 2026-09-21 · **Revised 2026-09-21** (red-team revision: 16 attacks
dispositioned in §10; coordinator rulings C, D2, `"jev"` identity, and the
escape-hatch requirement implemented throughout)

## 1. Goal and non-goals

### Goal

Define a provider-independent judgment layer in **goapauto core** so that Jev
(TypeSafe) is the *reference backend* and future judgment backends
(Instructor-structured LLM calls, Outlines grammars, small local LLMs) can
plug in **without touching goapauto core**. The deliverable is the interface
itself: core question/answer types, a `Judge` protocol, a backend-agnostic
error type, a generic `JudgmentSensor(Sensor)`, and a generic goal strategy —
all with zero third-party imports. A `JevJudge` adapter wraps the existing
SDK-bound pieces so `JevSensor` becomes one backend of the new layer while
its public surface stays frozen.

### Non-goals (what we choose NOT to build and why)

- **No new production backends.** No Instructor backend, no Outlines backend,
  no local-LLM backend. The interface must be proven against *something*
  non-Jev, and that proof is test-only: a `FakeJudge` in `goapauto.testing`
  plus a second, deliberately non-Jev-shaped rule-based test backend that
  does real translation work (Section 7). Building a second real backend now
  would anchor the design to two specific providers instead of the abstract
  shape.
- **No async API in Phase 1.** Judgment stays synchronous
  (`judge(...) -> ...`). This is acceptable — not merely convenient — for
  three reasons. (1) The agent loop is fully sync: planner, sensors, and
  arbitrator are all sync, so an async judge would force a sync-bridge or an
  async loop, both out of scope. (2) The caching layer (doc 02's ladder; this
  design's `min_interval`/change-detection) makes `judge()` rare relative to
  ticks: `sense()` is cache-cheap in the common case, so the amortized
  per-tick cost is bounded without concurrency. (3) Every backend bounds its
  own worst case with a transport timeout (`JevJudge` defaults to the
  long-standing 30s, configurable per backend). Accepted consequence, stated
  normatively: **a hung judgment backend blocks the sense→plan pipeline for
  its full transport timeout; no design (01, 03, or this one) provides an
  in-flight hook in Phase 1** — `latency_ms` is post-call attribution only.
  `AsyncJudge` (with cancellation semantics as a required part of its design,
  not an afterthought) is a named Phase-2 item (Section 8, Q1).
- **No batching API.** Jev's SDK already batches multiple questions per
  `system_one` call; the protocol naturally batches via
  `questions: Mapping[str, Question]`. A separate batch-of-states API is not
  needed for any known backend and would invent scheduling semantics.
- **No generalization beyond the three primitives — but with an explicit
  escape hatch.** We deliberately do NOT design an abstract "arbitrary typed
  question" system. The three primitives (`Noul`, `Choice`, `Score`) are the
  proven, narrow shape from Jev's production use, and the generic layers are
  honestly documented as **Jev-shaped**. Over-abstraction for backends that
  don't exist yet is the bigger risk — but fossilization is the counter-risk,
  so every question type carries a `metadata` side-channel and the extension
  rule is specified concretely (Section 2.1, "Escape hatch"). A future
  backend with a genuinely different shape gets a documented revision path,
  not a silent shoehorn and not a closed door.
- **No migration of `JevSensor` internals in Phase 1.** Phase 1 *defines* the
  interface; the adapter (`JevJudge`) can exist alongside `JevSensor` without
  rewriting it. The delegation decision is specified below (Section 2.6) with
  achievable acceptance criteria, but the rewrite itself is Phase 2.
- **No changes to `Sensor`, `GoalSelectionStrategy`, `SensorManager`,
  `GoalArbitrator`, or the planner.** The new layer plugs into existing seams.

## 2. User-side API

All new core symbols live in a new module `goapauto/models/judgment.py` (name
chosen over `judge.py` to avoid collision with `JevSensor.judge`, the method).
It imports stdlib + pydantic only. Public symbols are re-exported from
`goapauto/__init__.py` and added to `__all__` (eager import — no laziness
needed since there are no third-party imports).

### 2.1 Core question types — Jev-shaped, with an escape hatch

Three frozen dataclasses. They are Jev-shaped by design and documented as
such; an Instructor backend maps them to graded/`Literal`/ranged response
models, an Outlines backend to grammar-constrained outputs. The field names
mirror the SDK's question parameters so the Jev adapter is a mechanical
translation.

```python
@dataclass(frozen=True)
class NoulQuestion:
    """Binary truth judgment → float in [0, 1]."""
    instructions: str
    metadata: Mapping[str, Any] | None = None

@dataclass(frozen=True)
class ChoiceQuestion:
    """Pick exactly one label → the selected label (str)."""
    instructions: str
    choices: tuple[str, ...]
    # Optional per-choice descriptions, mirroring Jev's `criteria`.
    descriptions: Mapping[str, str] | None = None
    metadata: Mapping[str, Any] | None = None

@dataclass(frozen=True)
class ScoreQuestion:
    """Graded judgment on a rubric → float."""
    instructions: str
    min_value: float = 0.0
    max_value: float = 1.0
    rubric: str | None = None  # e.g. "0=calm … 1=panic"
    metadata: Mapping[str, Any] | None = None

Question = NoulQuestion | ChoiceQuestion | ScoreQuestion
```

**Escape hatch (concrete).** The three primitives are Jev-shaped by deliberate
decision, not by necessity: every question type carries `metadata`, a
backend-namespaced side-channel (`"jev.question"` holds the original SDK
question for round-trip identity; future backends use `"<backend>.<key>"`).
Generic layers (`JudgmentSensor`, `JudgmentGoalStrategy`) never interpret
`metadata` — they pass it through untouched — and they reject question types
outside the `Question` union with `TypeError` at construction. A backend
needing a fourth primitive does not extend this union: it defines its own
question dataclass, its own `Judge` implementation (the protocol's `questions`
annotation documents the generic-layer contract, not a closed world), and
either its own thin sensor/strategy or a core revision proposal. The
interface therefore doesn't fossilize: Jev-shaped is the default path,
`metadata` is the per-question escape hatch, and a foreign primitive is a
documented revision, not a shoehorn.

Design notes:

- `instructions` is positional-first on all three so positional construction
  reads naturally.
- `ChoiceQuestion.choices` is a `tuple`, not a list: hashable, immutable, and
  it makes duplicate detection and ordering deterministic. Duplicate labels
  are rejected at construction with `ValueError` (same rule as
  `JevGoalStrategy`'s unique-name requirement, generalized).
- `ScoreQuestion` uses `min_value`/`max_value` instead of Jev's legend-based
  scores: legends are a Jev-SDK rendering detail; a numeric interval is the
  provider-independent quantity. The Jev adapter converts the interval to a
  legend internally (Section 2.5 specifies the exact derivation rule); for
  questions that originated as SDK questions, the original legend rides in
  `metadata["jev.question"]` and is reused verbatim, so the Jev path has no
  round-trip loss.
- `metadata` defaults to `None`; generic layers treat `None` and `{}` the
  same (no metadata). Adapters must namespace their keys.

### 2.2 Core answer types — value + optional confidence

```python
@dataclass(frozen=True)
class FloatAnswer:
    """Graded/truth value → float. Serves NoulQuestion and ScoreQuestion;
    the *question* type decides the valid range, not the answer type."""
    value: float
    confidence: float | None = None

@dataclass(frozen=True)
class ChoiceAnswer:
    value: str
    confidence: float | None = None

Answer = FloatAnswer | ChoiceAnswer
```

- `NoulAnswer` and `ScoreAnswer` are merged into one `FloatAnswer`: they were
  structurally identical (`value: float`, `confidence: float | None`), and
  the correspondence check keys off the question type anyway. One fewer
  parallel hierarchy to keep in sync.
- `confidence` is `Optional[float]` because some backends cannot produce it
  (the SDK's `NoulAnswer` has no confidence field at all; a rule-based
  backend has no calibrated number). `None` means "backend did not report
  confidence", NOT "zero confidence".
- **No clamping anywhere — fail-loud beats silent coercion** (Ruling C).
  When present, confidence MUST be a float in `[0, 1]`; `NoulQuestion`
  answers MUST have value in `[0, 1]`; `ScoreQuestion` answers MUST have
  value in `[min_value, max_value]`. Adapters MUST NOT clamp out-of-range
  values into range: the adapter raises `JudgmentError(retryable=False)` at
  extraction, and the generic layers VERIFY the ranges on every received
  answer and raise `JudgmentError(retryable=False)` on violation (defense in
  depth — a third-party adapter that fails to validate is caught here,
  loudly, at the point of detection). Clamping twice would hide adapter bugs;
  validating twice hides nothing.
- Answer type must correspond to question type (`NoulQuestion`/`ScoreQuestion`
  → `FloatAnswer`, `ChoiceQuestion` → `ChoiceAnswer`). A backend returning a
  mismatched answer is a *backend bug*; the generic layers raise
  `JudgmentError(retryable=False)` (Section 2.4) rather than coercing
  silently.

### 2.3 The `Judge` protocol

```python
@runtime_checkable
class Judge(Protocol):
    """A judgment backend: answers typed questions about a state snapshot."""

    backend: str  # backend identity, e.g. "jev", "fake", "rule"

    def judge(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Question],
    ) -> JudgmentResponse:
        """Answer each named question about `state`.

        `questions` must be non-empty. Implementations must answer every
        question or raise `JudgmentError`; partial responses are not allowed
        (the caller cannot distinguish "unanswered" from "missing" anyway).
        """
        ...

    def close(self) -> None:
        """Release resources. No-op for stateless backends. Idempotent."""
        ...
```

`JudgmentResponse` is a small frozen dataclass (not a raw dict) so backend
identity and metadata ride along for diagnostics (Section 6e):

```python
@dataclass(frozen=True)
class TokenUsage:
    """Backend-reported token consumption. Adapters MUST populate both
    fields when the backend reports them; None when it does not."""
    input_tokens: int | None = None
    output_tokens: int | None = None

@dataclass(frozen=True)
class JudgmentResponse:
    answers: Mapping[str, Answer]          # same keys as the asked questions
    backend: str                          # e.g. "jev", "fake", "rule"
    model: str | None = None              # backend's model id, if any
    latency_ms: float | None = None       # measured by the backend/adapter
    usage: TokenUsage | None = None       # typed token usage (no key-guessing)
```

Rationale for the shape:

- **Sync only** for Phase 1 (see non-goals). A future async variant would be
  a separate protocol (`AsyncJudge`), not a flag on this one.
- `judge` takes the whole question mapping in one call — this IS the batching
  mechanism; no second API needed.
- `state` is a `Mapping[str, Any]`, mirroring `JevClient.system_one`'s first
  argument (the SDK already takes a free-form state object). Generic layers
  pass `dict(state)` defensively so a backend cannot mutate the caller's
  snapshot.
- `JudgmentResponse.answers` keys must equal the asked question names
  exactly. Generic layers validate this and raise
  `JudgmentError(retryable=False)` on a missing key — a backend silently
  dropping a question is a correctness bug, not a partial result. Extra keys
  are ignored defensively but logged at warning.
- `backend` is a plain string identifier (not an enum): enums would require
  core to know every backend in advance, defeating provider-independence.
  Convention: lowercase dotted name (`"jev"`, `"instructor-openai"`). The
  canonical name for the TypeSafe backend is `"jev"` — matching
  `JevSensor`/`JevGoalStrategy` naming, not `"typesafe"`.
- `usage` is a typed `TokenUsage`, not a raw mapping: the sensor fills
  `JudgmentCallRecord.input_tokens/output_tokens` from its fields, so
  adapters own the interpretation once, at the boundary, instead of the
  sensor guessing at backend-defined keys.
- `Judge.backend` is declared on the protocol so sensors can expose backend
  identity without importing backend types (`JudgmentSensor.backend_name()`).
  Note: `@runtime_checkable` `isinstance` checks only verify method presence;
  the `backend` attribute is a documented convention the sensor reads via
  normal attribute access, defaulting to `"unknown"` if absent.

### 2.4 Backend-agnostic error type

```python
class JudgmentError(Exception):
    """Any failure of a judgment backend.

    Raised by `Judge` implementations and by backend adapters (which
    translate backend-native errors into this at their boundary). Carries
    the backend identity so generic layers can log without importing
    backend-specific types.

    Attributes:
        backend: the backend identifier (mirrors `JudgmentResponse.backend`).
        retryable: PRECISE, not best-effort. True iff the failure is
            transient for this call; False iff it is permanent for this
            call. The generic layers' absorb-vs-propagate policy keys off
            this flag, so mislabeling has behavioral consequences —
            adapters must map, not guess (mapping table in Section 2.5).
    """

    def __init__(self, message: str, *, backend: str, retryable: bool) -> None: ...
```

**`retryable` definition (Ruling D2, binding).** `retryable=True` iff the
failure is transient: rate-limit/429, network timeout, connection error,
5xx/service-unavailable. `retryable=False` iff the failure is permanent for
this call: auth failure (invalid/revoked/missing key — 401/403), malformed
question (validation failure before any backend call), contract violation
(missing answer key, wrong answer type, out-of-range value/confidence).
`retryable` is required (no default): every raise site must decide, because
the generic sensor's error policy keys off it.

- This is the ONLY exception type generic layers (`JudgmentSensor`,
  `JudgmentGoalStrategy`) catch. **A `Judge` that raises any other exception
  type propagates it unchanged — fail-loud** (Ruling D2). Generic layers do
  not wrap-and-swallow: a backend bug (e.g. `AttributeError` from a miswired
  client, or a raw transport exception the adapter failed to translate)
  skips the stale-cache path, skips graceful degradation, and is visible as
  itself. Loudness is preserved by chaining: adapters SHOULD chain the
  original (`from exc`) so the traceback still shows the root cause.
- Telemetry error naming rule (used by the generic layers and the Phase-2
  frozen-surface translation): the recorded `error` name is
  `type(exc.__cause__).__name__` when the `JudgmentError` chains a cause,
  else `type(exc).__name__`. Rationale: the record should name the most
  specific available failure identity ("TypeSafeError", not the generic
  wrapper). This rule is backend-agnostic and deterministic.
- `TypeSafeError` remains the Jev-specific error and stays public; the
  adapter translates it at the boundary, never above it (Section 2.5).
  `JevSensor`'s existing `except TypeSafeError` handling is untouched in
  Phase 1 (frozen surface).
- Nothing auto-retries on `retryable`. The flag drives the generic layers'
  absorb-vs-propagate policy (Section 3) and the operator-facing
  logs/telemetry ("transient blip" vs "permanent for this call"). Retry with
  backoff, if ever wanted, belongs to the agent loop or a future resilience
  layer — never inside `sense()`.

### 2.5 The Jev adapter — `JevJudge`

Lives in `goapauto.models.jev` (it needs `typesafe_sdk`; core must not
import it). Wraps a `JevClient`:

```python
class JevJudge:
    """`Judge` adapter over a `JevClient` (TypeSafeClient or compatible)."""

    backend = "jev"  # class-level constant, mirrored into every response

    def __init__(self, client: JevClient | None = None) -> None:
        # Same default as JevSensor/JevGoalStrategy: TypeSafeClient(timeout=30.0).
        # Caller-owned vs owned lifecycle follows the existing convention:
        # a passed-in client is left open; a created one is closed by close().

    def judge(self, state, questions) -> JudgmentResponse: ...
    def close(self) -> None: ...
```

**Question translation (core → SDK).** For each named core question:

- `NoulQuestion(instructions, metadata)` → `metadata["jev.question"]` if
  present (verbatim reuse — the Phase-2 delegation path), else
  `Noul(instructions=instructions)`.
- `ChoiceQuestion(instructions, choices, descriptions, metadata)` →
  `metadata["jev.question"]` if present, else `Choice(instructions=...,
  criteria={label: (descriptions[label] if descriptions and label in
  descriptions else label) for label in choices})`.
- `ScoreQuestion(instructions, min_value, max_value, rubric, metadata)` →
  `metadata["jev.question"]` if present, else the canonical derivation:
  the SDK scores positions `0..n-1` ("each description's position determines
  its score, starting at zero"), so build `Score(instructions=rubric_text,
  criteria=[rung_0 … rung_{n-1}])` with `n = max(2, round(max_value -
  min_value) + 1)` rungs, where rung `i` corresponds to value `min_value +
  i·(max_value − min_value)/(n−1)`. Rung labels are the formatted rung
  values (`f"{v:g}"`); when `rubric` is present it is appended to the
  instructions as `f"{instructions}\nRubric: {rubric}"`. The derivation is
  deterministic and pinned by tests; prompt *quality* for fresh
  `ScoreQuestion`s is the adapter author's concern, prompt *identity* for
  Jev-originated questions is guaranteed by the metadata path.

**Answer extraction (SDK → core).** The whole client-call + extraction block
is wrapped so that any `TypeSafeError` raised below the boundary (SDK
client, shared `jev.py` helpers such as doc 04's confidence extraction) is
translated; nothing named `TypeSafeError` crosses `JevJudge.judge` outward:

- `NoulQuestion` → `response.nouls[name].noul` → `FloatAnswer(value, None)`.
  The SDK's `NoulAnswer` carries no confidence field, so Noul confidence is
  always `None` — the derived-decisiveness proxy (`2·|noul−0.5|`) is
  dropped: it was uncalibrated invention, and doc 04's own review recommends
  cutting it.
- `ChoiceQuestion` → `response.choices[name].choice` /
  `.confidence` → `ChoiceAnswer(value, confidence)`. Confidence is
  SDK-reported and validated: out-of-range (or NaN) → `JudgmentError(...,
  retryable=False)` raised directly by the adapter — never clamped, never
  coerced into `TypeSafeError`.
- `ScoreQuestion` → `response.scores[name].score` / `.confidence` → the SDK
  score `s` lives on `[0, n−1]` (n = rung count of the asked question);
  validate `0 ≤ s ≤ n−1` (fail-loud on violation), then map to the core
  interval: `v = min_value + s·(max_value − min_value)/(n−1)` →
  `FloatAnswer(v, confidence)`. SDK-reported confidence is validated like
  Choice's.
- Missing answer: `KeyError` from the SDK's typed accessors (this is also
  the garbled-answer path — the accessors filter by `isinstance`, so a
  wrong-typed answer surfaces as a missing key) is re-expressed as
  `TypeSafeError(f"Missing answer for question {name!r}")` — matching the
  frozen `JevSensor._extract` behavior exactly — and then translated like
  any `TypeSafeError` (below).
- Wrong SDK response type: `_require_response`'s `TypeError` propagates
  unchanged (programmer error, fail-loud — same as frozen).
- Non-`SystemOneResponse`, non-`TypeSafeError` exceptions from the client
  (raw transport leaks, mock miswiring) propagate unchanged per Ruling D2;
  the adapter does not wrap what it does not recognize.

**`TypeSafeError` → `JudgmentError` translation (exact).** Caught inside
`judge()`; re-raised as `JudgmentError(str(exc), backend="jev",
retryable=mapped)` with `from exc` chaining. The mapping (checked
most-specific-first; grounded in the SDK's error hierarchy):

| SDK signal | `retryable` | D2 class |
|---|---|---|
| `TypeSafeRateLimitError` (429) | True | rate-limit |
| `TypeSafeAPITimeoutError` | True | network timeout |
| `TypeSafeAPIConnectionError` | True | transient transport |
| `TypeSafeInternalServerError` (5xx) | True | 5xx |
| `TypeSafeAPIError` with `status` 429 / 408 / ≥500 | True | rate-limit / 5xx |
| `TypeSafeAuthenticationError` (401), `TypeSafePermissionDeniedError` (403) | False | auth failure |
| `TypeSafeBadRequestError` (400), `TypeSafeNotFoundError` (404), `TypeSafeUnprocessableEntityError` (422) | False | malformed question / permanent |
| `TypeSafeAPIResponseValidationError` | False | contract violation (response shape) |
| `TypeSafeAPIError` with other 4xx `status` | False | permanent for this call |
| base `TypeSafeError` (no subclass/status signal) | False | unclassifiable — D2's biconditional forbids labeling unknown as transient; fail loud with the original chained |

The adapter's own validation failures (empty questions, unknown question
type at translation, missing answer key, wrong answer type, out-of-range
value/confidence) raise `JudgmentError(..., backend="jev", retryable=False)`
directly — no `TypeSafeError` is involved above the boundary.

`close()` follows the ownership rule: closes the client only if this
`JevJudge` created it; idempotent.

### 2.6 Generic `JudgmentSensor(Sensor)` — the caching layer lives here

```python
class JudgmentSensor(Sensor):
    """World-state sensor backed by any `Judge`.

    Same cache/staleness/telemetry contract as `JevSensor`, generalized:
    questions are core `Question` objects, answers are mapped onto
    world-state keys via `mapping`, and the backend is any `Judge`.

    This is where the caching/staleness layer (differentiator #2) lives:
    ABOVE the protocol, so every backend inherits it. The protocol itself
    guarantees nothing about caching — a bare `Judge.judge` call always
    goes to the backend.
    """

    def __init__(
        self,
        judge: Judge,
        observe: Callable[[], dict[str, Any]],
        questions: Mapping[str, Question],
        mapping: dict[str, str] | None = None,
        min_interval: float = 0.0,
        resense_on_change: bool = True,
        max_stale: float = 30.0,
        telemetry: Callable[[JudgmentCallRecord], None] | None = None,
        fail_loud: bool = True,
    ) -> None:
        ...

    def sense(self) -> dict[str, Any]: ...
    def judge(self, observation: dict[str, Any]) -> dict[str, Any]: ...
    def stats(self) -> JudgmentStats: ...
    def backend_name(self) -> str:
        """Backend identity for diagnostics (doc 06's sensor-level hook):
        returns `self._judge.backend`, or "unknown" if the judge lacks it."""

    def close(self) -> None:
        """Close the judge. `JudgmentSensor` never creates the judge (it is
        a required constructor arg), so `close()` ALWAYS delegates to
        `judge.close()`. Documented explicitly because it differs from
        `JevSensor`'s conditional close."""
```

Semantics are ported 1:1 from `JevSensor` (Section 3 pins the details),
with two specified differences:

- Answer → state-value extraction: `FloatAnswer.value` → float,
  `ChoiceAnswer.value` → label string. The *question type fixed at
  construction* decides the expected answer type and the valid range; a
  mismatched answer or an out-of-range value raises
  `JudgmentError(retryable=False)`.
- Cache: re-judge when observation changes (`resense_on_change`) or
  `min_interval` elapsed. **Error policy (explicit):** on `JudgmentError`
  during a re-judge, if the error is retryable the sensor absorbs it into
  the stale-cache path (reuse `self._cached` while `now − last_success ≤
  max_stale`, else return `{}`); if the error is non-retryable and
  `fail_loud` is true (the default), the sensor records telemetry and
  re-raises; if `fail_loud` is false, the non-retryable error is absorbed
  exactly like a retryable one. Non-`JudgmentError` exceptions are never
  caught. Telemetry is recorded for EVERY attempted `judge()` call —
  including calls whose error then propagates — so `stats()` never drifts
  from reality. Telemetry records mirror `JevCallRecord`/`JevStats` as
  `JudgmentCallRecord`/`JudgmentStats` with a `backend: str` field added;
  the record's `error` name follows the cause-chain rule (Section 2.4).
- `mapping` defaults to identity; unknown mapping keys raise `ValueError`
  at construction (Section 3).
- `fail_loud=False` exists for exactly one consumer: the Phase-2 `JevSensor`
  delegation, which must reproduce the frozen 0.5.0 absorb-everything
  policy. New code must not use it; it is the documented legacy-
  compatibility branch, not a second policy to choose between.

**Migration decision (JevSensor delegation — Phase 2, conditional).**
`JevSensor` keeps its exact public constructor and behavior (frozen).
Internally, Phase 2 MAY delegate to `JudgmentSensor` with an internal
`JevJudge` adapter and `fail_loud=False`, rather than maintaining two
caching implementations. Justification: the caching logic
(change-detection, staleness windows, telemetry, thread-safety caveats) is
subtle and already debugged in `JevSensor`; two copies will drift — the
first behavioral fix applied to one and not the other creates a silent
contract fork. Delegation MAY proceed once the characterization tests in
Section 7 are written and green against the non-delegating implementation;
it is not "SHOULD" — it is conditional on the acceptance criteria below,
which are achievable as specified (no punted open questions gate it):

1. **Prompt identity (no round-trip loss).** `JevSensor` translates its SDK
   questions to core questions with the original SDK question object in
   `metadata["jev.question"]` (`Score`: `criteria` list →
   `min_value=0.0`, `max_value=float(len(criteria)−1)`, `rubric=";
   ".join(criteria)`; `Choice`: dict-valued criteria preserved via the
   original; `Noul`: instructions only). `JevJudge` reuses the metadata
   original verbatim, so the SDK questions forwarded to `system_one` are
   identical to the frozen path. Pinned by
   `test_jev_sensor_delegation_question_identity` and
   `test_jev_goal_strategy_delegation_prompt_identity`, which assert on the
   exact question objects recorded by `FakeTypeSafeClient.calls`.
2. **Exception contract preserved.** The generic layer raises `ValueError`
   for construction violations (empty questions, unknown mapping keys);
   `JevSensor`'s frozen contract raises `TypeSafeError` there. The
   delegating `JevSensor.__init__` catches `ValueError` from the inner
   construction and re-raises `TypeSafeError(str(exc)) from exc` at its own
   boundary. Runtime `JudgmentError`s from the inner path (with
   `fail_loud=False`, these are only the absorbed ones — none escape) never
   surface; the frozen absorb policy (stale reuse / `{}`) is reproduced by
   the inner layer's `fail_loud=False` branch, and the frozen telemetry
   error names (`"TypeSafeError"`) are reproduced by the cause-chain naming
   rule (the adapter chains the original `TypeSafeError`).
3. **Client lifecycle on construction failure.** `JevSensor.__init__` today
   validates before creating the client. Under delegation the order is:
   build `JevJudge(client)` (creates a `TypeSafeClient` when `client=None`)
   → construct inner `JudgmentSensor` (validates). If the inner
   construction raises, the delegating `__init__` MUST call `judge.close()`
   (ownership-aware: closes only the client it created) before re-raising
   the translated `TypeSafeError` — no leaked connection pool. Pinned by
   `test_jev_sensor_delegation_construction_failure_closes_client`.
4. **`shared_client` yields a `TypeSafeClient`** which satisfies `JevClient`
   — still accepted, unchanged.
5. **Thread-safety caveat** stays "not thread-safe, one per thread".
6. **Telemetry value-identity.** The `JevCallRecord` stream emitted under
   delegation is value-identical to 0.5.0 for identical scenarios:
   field-for-field translation from `JudgmentCallRecord` (which carries the
   cause-chain-resolved error name), pinned by
   `test_jev_sensor_delegation_error_name_preserved`
   (`records[0].error == "TypeSafeError"`) and the existing
   `test_sensor_records_failure*` / `test_strategy_records_failure` suites,
   which run unmodified.

Risk acknowledged: delegation makes `JevSensor` depend on the new generic
path, so a bug in `JudgmentSensor` breaks the frozen surface. Mitigation:
the existing `JevSensor` test suite plus the new characterization tests run
against the delegating implementation in Phase 2 — they are the regression
net, and the "unchanged suite" is necessary but not sufficient (prompt
identity and lifecycle are pinned by the NEW tests, not the old ones).

### 2.7 Generic goal strategy — `JudgmentGoalStrategy`

```python
class JudgmentGoalStrategy:
    """`GoalSelectionStrategy` over any `Judge`.

    Builds one `ChoiceQuestion` with a label per goal (label = goal name,
    falling back to `str(goal.target_state)`; descriptions carry the JSON-safe
    target state + priority, mirroring `JevGoalStrategy`'s criteria). Asks the
    judge, returns the chosen goal. On retryable `JudgmentError` (or an
    unknown label), falls back to the first goal — same policy as
    `JevGoalStrategy`. On non-retryable `JudgmentError`, propagates when
    `fail_loud` is true (default), else falls back like a retryable error.
    """

    def __init__(
        self,
        judge: Judge,
        instructions: str = "Which goal should the agent pursue next?",
        telemetry: Callable[[JudgmentCallRecord], None] | None = None,
        fail_loud: bool = True,
    ) -> None: ...

    def select(self, goals: list[Goal], state: WorldState) -> Goal | None: ...
    def stats(self) -> JudgmentStats: ...
    def backend_name(self) -> str: ...  # same hook as the sensor
    def close(self) -> None: ...  # delegates to judge.close(), same rule as sensor
```

`JevGoalStrategy` stays frozen as the Jev-specific convenience (it owns its
client lifecycle and its `TypeSafeError` handling). It MAY delegate to
`JudgmentGoalStrategy(JevJudge(client), fail_loud=False)` internally in
Phase 2 under the same acceptance criteria as the sensor (prompt identity
via `metadata["jev.question"]` carrying the original dict-valued criteria —
no JSON round-trip touches the model prompt; telemetry/error-name
translation via the cause-chain rule; exception contract preserved). The
duplicate-goal-name `ValueError` stays exactly as-is.

### 2.8 Example usage

```python
from goapauto import (
    JudgmentSensor, JudgmentGoalStrategy,       # core, no third-party imports
    NoulQuestion, ChoiceQuestion, ScoreQuestion,
    JudgmentError,
)
from goapauto.models.jev import JevJudge        # needs the [jev] extra

# Any Judge works here. Jev is the reference backend:
judge = JevJudge()  # defaults to TypeSafeClient(timeout=30.0)

sensor = JudgmentSensor(
    judge=judge,
    observe=lambda: {"enemies": camera.count_hostiles(), "hp": player.hp},
    questions={
        "danger": NoulQuestion("Is the agent in immediate danger?"),
        "stance": ChoiceQuestion(
            "How should the agent posture?",
            choices=("fight", "flight", "hide"),
            descriptions={"fight": "engage", "flight": "run", "hide": "break line of sight"},
        ),
        "threat": ScoreQuestion("Rate the threat level", min_value=0.0, max_value=10.0,
                                rubric="0=safe … 10=lethal"),
    },
    mapping={"danger": "danger", "stance": "stance", "threat": "threat_level"},
    min_interval=1.0,
    max_stale=30.0,
)

strategy = JudgmentGoalStrategy(judge=judge)
arbitrator = GoalArbitrator(goals=[...], strategy=strategy)

try:
    updates = sensor.sense()
except JudgmentError as exc:
    # Under the default fail_loud=True policy, a JudgmentError escaping
    # sense() is always non-retryable (contract violation, auth failure,
    # malformed question) or a non-JudgmentError backend bug — never a
    # transient blip. Retryable failures are absorbed into the stale-cache
    # path and surface as {} (dead cache), not as exceptions.
    logger.error("judgment backend misconfigured or failing permanently: %s", exc)

# Swapping backends is a one-line change; nothing else in the agent moves:
# judge = InstructorJudge(model="gpt-4o-mini")   # future backend
# sensor = JudgmentSensor(judge=judge, observe=..., questions=...)
```

A non-Jev backend sketch (what a future author implements — no core changes):

```python
class InstructorJudge:  # lives OUTSIDE goapauto core, e.g. goapauto_instructor/
    backend = "instructor-openai"

    def __init__(self, client, model: str): ...

    def judge(self, state, questions) -> JudgmentResponse:
        # Map each core Question to an instructor response_model:
        #   NoulQuestion   -> float field with ge=0, le=1  (graded truth, NOT bool:
        #                     collapsing to {0.0, 1.0} would be semantic narrowing
        #                     the backend author must justify, not assume)
        #   ChoiceQuestion -> Literal[choices]
        #   ScoreQuestion  -> float with ge=min_value, le=max_value
        # Confidence: logprob-derived, or None when the backend cannot
        #   produce it (then the gating layer's absent-confidence policy
        #   applies — Section 6d).
        # On validation failure: raise JudgmentError(..., backend=self.backend,
        #                                           retryable=False)

    def close(self) -> None: ...
```

## 3. Precise semantics, edge cases, and defaults

### Construction validation (generic layers)

All construction-time violations raise `ValueError` — never `JudgmentError`.
`JudgmentError` is reserved for runtime backend failures; conflating
fail-fast config validation with tolerate-outage handling would force
callers to inspect `retryable` to tell "my config is wrong" from "the
backend is down".

| Input | Rule | Error |
|---|---|---|
| `questions` empty | Rejected — a judgment call with no questions is meaningless | `ValueError` |
| Question of unknown type | Rejected at construction | `TypeError` (programmer error, mirrors `JevSensor`) |
| `mapping` key not in `questions` | Rejected at construction | `ValueError` |
| `mapping` value (state key) colliding with an existing key | Allowed — last write wins per `SensorManager.update_state` semantics; documented, not validated |
| `max_stale < 0` | Rejected | `ValueError` (mirrors `JevSensor`) |
| Duplicate labels in `ChoiceQuestion.choices` | Rejected at construction | `ValueError` |
| `ScoreQuestion` with `min_value >= max_value` | Rejected at construction | `ValueError` |
| `ChoiceQuestion` with empty `choices` | Rejected at construction | `ValueError` |
| `judge=None` | Rejected — the judge is required, there is no default backend in core | `TypeError` (missing required arg; no sentinel) |

Note on the `JevSensor` frozen contract: `JevSensor` raises `TypeSafeError`
for empty questions / unknown mapping keys today. The generic layer raises
`ValueError`; under the Phase-2 delegation `JevSensor` catches the
`ValueError` at its own boundary and re-raises `TypeSafeError` — this is
acceptance criterion #2 in Section 2.6, not a Phase 1 behavior change.

### `judge()` call semantics (the protocol)

1. **State snapshot:** generic layers pass `dict(state)` — a shallow copy —
   so backends cannot mutate the caller's observation. Backends must treat
   `state` as read-only regardless.
2. **Questions snapshot:** generic layers pass `dict(questions)`; backends must
   not retain or mutate it.
3. **Completeness:** the response's `answers` keys must equal the question
   names exactly. Missing key → `JudgmentError(retryable=False)` from the
   generic layer (backend contract violation). Extra keys → ignored
   defensively, but logged at warning (a backend answering more than asked
   is suspicious, not fatal).
4. **Answer-type correspondence and range verification:** `NoulQuestion`→
   `FloatAnswer` with value ∈ [0,1]; `ScoreQuestion`→`FloatAnswer` with
   value ∈ [`min_value`, `max_value`]; `ChoiceQuestion`→`ChoiceAnswer`;
   confidence ∈ [0,1] or `None` whenever present. The generic layer VERIFIES
   all of this on every received answer and raises
   `JudgmentError(retryable=False)` on violation — no clamping, no coercion
   (Ruling C). Adapters are expected to validate at extraction too;
   validating twice hides nothing.
5. **No caching in the protocol.** Every `Judge.judge` call is a real backend
   call. Caching lives only in `JudgmentSensor` (differentiator #2, Section
   6b). A backend MAY memoize internally (e.g. a deterministic rule-based
   judge) but must not advertise staleness semantics through the protocol —
   there is no field for it, deliberately.
6. **Exceptions:** the only catchable-by-generic-layers exception is
   `JudgmentError`. Backend-native exceptions propagate unwrapped (Ruling
   D2). Rationale: wrapping everything would let a backend bug (e.g.
   `AttributeError` from a miswired client) masquerade as a backend outage
   and get silently absorbed by the stale-cache path. Fail loudly on bugs;
   degrade gracefully on declared outages.
7. **Telemetry naming:** the generic layers record the error name per the
   cause-chain rule — `type(exc.__cause__).__name__` when the
   `JudgmentError` chains a cause, else `type(exc).__name__`.
8. **`close()`** is idempotent and safe to call on a never-used judge.
   `JudgmentSensor.close()` / `JudgmentGoalStrategy.close()` always delegate
   (they never create the judge, so there is no ownership ambiguity).

### `JudgmentSensor.sense()` / `.judge(observation)` semantics

Ported 1:1 from `JevSensor` (this is the "no two implementations drift"
requirement — the generic version is the canonical one going forward),
with the error policy below replacing the frozen absorb-everything rule:

- First call always judges (no cached observation).
- Re-judge iff `resense_on_change and observation != last_observation`, or
  `now - last_call >= min_interval`. `observation` equality is plain dict
  `!=` (same as `JevSensor`; NaN-containing observations will always compare
  unequal — documented quirk, inherited, not fixed here).
- `min_interval=0.0` (default): re-judges on EVERY call even with an
  unchanged observation (`(now − last_call) >= 0.0` is always true once the
  change-check passes it through). This matches `JevSensor._should_resense`
  exactly — surprising but frozen; set `min_interval > 0` to throttle.
- **Error policy (the canonical sentence — §4 states it identically):**
  on `JudgmentError` during a re-judge, if the error is retryable the sensor
  absorbs it into the stale-cache path (reuse `self._cached` while
  `now − last_success ≤ max_stale`, logging a warning, else log an error
  and return `{}`); if the error is non-retryable and `fail_loud` is true
  (the default), the sensor records telemetry and re-raises; if
  `fail_loud` is false, the non-retryable error is absorbed exactly like a
  retryable one. Non-`JudgmentError` exceptions are never caught.
  `max_stale=0.0` means "never reuse" — any absorbed failure returns `{}`
  immediately.
- Telemetry is recorded for EVERY attempted `judge()` call, including calls
  whose error then propagates — `stats()` never drifts from reality.
- Successful re-judge replaces the whole cache atomically (no partial updates).
- `sense()` and `judge(observation)` share cache state — do not mix them on
  one instance (inherited caveat, documented on both methods).
- Thread safety: not thread-safe; one instance per thread (inherited).

### `JudgmentGoalStrategy.select()` semantics

- `goals == []` → return `None` (no judgment call made).
- Label = `goal.name if goal.name is not None else str(goal.target_state)`;
  duplicate labels → `ValueError` (before any backend call).
- Descriptions = `{label: {"target_state": <json-safe>, "priority": ...}}`
  serialized into the `ChoiceQuestion.descriptions` mapping as strings
  (JSON dump of the per-goal dict — the adapter decides rendering). For the
  Jev path the original dict-valued criteria ride in
  `metadata["jev.question"]` and are reused verbatim (no prompt drift).
- **Error policy:** on `JudgmentError` during selection, if the error is
  retryable — or if `fail_loud` is false — log a warning and return
  `goals[0]` (inherited policy); if the error is non-retryable and
  `fail_loud` is true (the default), record telemetry and re-raise.
- Backend picks an unknown label → log warning, return `goals[0]`
  (inherited policy). Note: this is distinct from the sensor's `{}` policy —
  goal selection must always return *a* goal to keep the loop running, while
  a sensor may legitimately report "no updates".
- No caching (inherited: `JevGoalStrategy` never cached; goal selection runs
  at arbitration frequency and the arbitrator already filters satisfied
  goals).

## 4. Failure-mode table

The handling column below states the **generic-layer** policy, word-for-word
identical to Section 3's canonical sentence. `JevSensor`'s frozen 0.5.0
behavior differs in exactly one row (marked FROZEN) and is specified
separately — the two policies are not conflated.

| Failure mode | Where detected | Handling |
|---|---|---|
| Backend unreachable / timeout / 5xx → `JudgmentError(retryable=True)` | `Judge.judge` / adapter | Sensor: absorb into stale-cache path — reuse cache while `now − last_success ≤ max_stale` (warning), else `{}` (error log). Strategy: warning + `goals[0]`. |
| Backend auth/config error (bad key) → `JudgmentError(retryable=False)` | Adapter (`TypeSafeAuthenticationError` → `retryable=False`) | Sensor (default `fail_loud=True`): record telemetry, re-raise — a bad key is permanent for this call and must not be silently absorbed. Strategy: record telemetry, re-raise. |
| Backend returns wrong answer types / drops a question / out-of-range value or confidence → `JudgmentError(retryable=False)` | Generic-layer validation | Same as above: record telemetry, re-raise under the default policy (contract violations are bugs, not outages — fail loud). |
| Backend raises non-`JudgmentError` (bug, raw transport leak) | — | Propagates unwrapped through generic layers (Ruling D2). Loud failure by design. |
| `TypeSafeError` from SDK | `JevJudge.judge` boundary | Translated to `JudgmentError(backend="jev", retryable=<Section 2.5 table>)` with `from exc` chaining. Never leaks past the adapter. |
| FROZEN — `JevSensor` (0.5.0, unchanged in Phase 1): ANY `TypeSafeError`, including contract violations (missing/garbled answers) | `JevSensor._judge` | Absorb into stale-cache path (reuse while fresh, else `{}`). Pinned by `test_missing_answer` and `test_garbled_answer_degrades_gracefully`. Under Phase-2 delegation this exact behavior is reproduced by the inner layer's `fail_loud=False` branch — it is not the generic default. |
| Observation contains NaN | `_should_resense` dict comparison | Always unequal → re-judges every call. Documented quirk, inherited from `JevSensor`. |
| Telemetry callback raises | `_record_telemetry` | Caught, `logger.exception`, ignored (inherited). Telemetry must never break judgment. |
| `close()` called twice / on unused judge | `close()` | Idempotent no-op after first. |
| Empty questions / unknown mapping key at construction | Constructor | `ValueError` in generic layers (Section 3). `JevSensor` keeps raising `TypeSafeError` via boundary translation (Phase 2, criterion #2). |
| Cache older than `max_stale` on absorbed failure | `_judge` | Return `{}` + error log. The agent sees *no updates*, not *wrong updates*. |

## 5. What the protocol guarantees vs. what layers add

(The boundary contract, stated once and referenced by the other designs.)

**The `Judge` protocol guarantees:**
- Typed answers for every asked question, or a `JudgmentError`. Nothing
  else: no caching, no staleness, no retries, no fallbacks, no telemetry.
- Answer-type correspondence and verified ranges (adapters validate at
  extraction; generic layers verify — neither clamps).
- Backend identity (`backend`, `model`) on every response for diagnostics.

**Generic layers add (above the protocol, inherited by all backends):**
- `JudgmentSensor`: change-detection, `min_interval`/`max_stale` caching,
  stale-reuse degradation, per-call telemetry + aggregate stats, answer→
  state-key mapping, the absorb-vs-propagate error policy.
- `JudgmentGoalStrategy`: label construction, duplicate detection,
  first-goal fallback policy, per-call telemetry.
- Neither layer retries. Neither layer swallows non-`JudgmentError`
  exceptions.

**Where the caching layer lives (boundary diagram — one picture, no
ambiguity):**

```
SensorManager.update_state()
        │  sense() -> dict[str, Any]            (Sensor interface)
        ▼
┌─ doc 02 ────────────────────────────┐
│ CachingSensor(Sensor)               │  "outer sensor cache"
│ per-key staleness ladder            │  wraps the Sensor interface;
│ OPTIONAL — any sensor               │  redundant under a JudgmentSensor
└──────────────┬──────────────────────┘
               │ sense() -> dict[str, Any]
┌─ doc 05 ─────▼──────────────────────┐
│ JudgmentSensor(Sensor)              │
│   ┌───────────────────────────────┐ │  "inner judgment cache"
│   │ change-detect / min_interval  │ │  wraps the Judge protocol;
│   │ / max_stale                   │ │  THE canonical judgment cache.
│   │ (+ doc-02 ladder when it      │ │  Doc 02's per-key ladder is
│   │   lands: THE retarget point)  │ │  re-targeted HERE, not duplicated.
│   └──────────────┬────────────────┘ │
└──────────────────┼───────────────────┘
                   │ judge(state, questions) -> JudgmentResponse
┌──────────────────▼───────────────────┐
│ Judge protocol                       │  NO caching, NO staleness,
│ pure function of (state, questions)  │  NO retries — typed answers
└──────────────────┬───────────────────┘  or JudgmentError, nothing else
                   │
┌──────────────────▼───────────────────┐
│ JevJudge (adapter, [jev] extra)      │  TypeSafeError -> JudgmentError
└──────────────────┬───────────────────┘  at the boundary (Sec 2.5)
                   │ system_one(state, sdk_questions)
                   ▼
               typesafe_sdk
```

Named relationship: doc 02's `CachingSensor` wraps the **Sensor interface**
(above `sense()`); doc 05's inner judgment cache wraps the **Judge
protocol** (below `sense()`). They are two composed layers, not the same
layer. Composition rules:

- Doc 02's `judge(observation) -> dict` protocol sketch YIELDS to doc 05's
  `judge(state, questions) -> JudgmentResponse` (coordinator position): the
  protocol returns typed answers; extraction to state values happens in the
  generic layer above it.
- Doc 02's per-key staleness ladder is re-targeted as the
  retention/write rule OF the inner judgment cache — there is exactly one
  judgment caching implementation. Until doc 02 lands, the specified
  behavior is the 1:1 whole-cache port in Section 3. Doc 05 owns the cache's
  placement and its external knobs (`min_interval`, `max_stale`,
  `resense_on_change`); doc 02 owns the per-key write/retention rule applied
  inside it, and must preserve the frozen observable contract for the Jev
  path when it lands (its own review already requires this).
- `CachingSensor(JudgmentSensor(...))` is legal but redundant: the outer
  cache would re-cache `sense()` outputs the inner cache already manages.
  Recommended configuration is the inner cache alone for judgment sensors;
  the outer layer remains for non-judgment sensors.

## 6. Interaction notes with the other five differentiators

### (a) Runtime / replan budgets

The protocol is budget-agnostic: `Judge.judge` has no timeout parameter.
Budgets are enforced *around* judgment, not inside it — the sensor's
`min_interval` already rate-limits calls, and the replan-budget design
treats a judgment call as a timed block like any other. `JudgmentResponse`
carries `latency_ms` (measured by the adapter) so budget accounting can
attribute time precisely — but stated honestly: `latency_ms` exists only
*after* the call returns. There is no mechanism for the budget layer (doc 01)
to time-box or observe an in-flight `judge()` in Phase 1; doc 01 must not
assume a hook exists. A backend that needs its own deadline configures it in
its own constructor (e.g. `JevJudge(client=TypeSafeClient(timeout=8.0))`,
or `InstructorJudge(timeout=...)`) — the protocol does not standardize
timeouts because "deadline" means different things per transport.

### (b) Sensor caching / staleness — THE caching layer lives here

Defined precisely in Section 5 (diagram included): caching is a property of
`JudgmentSensor`'s inner cache, never of the protocol. Consequences for the
caching design:

- The caching design must NOT add cache fields to `JudgmentResponse` or
  cache parameters to `Judge.judge` — that would let backends and the sensor
  disagree about freshness.
- Staleness telemetry (`stale_cache_hit` in `JudgmentCallRecord`) is emitted
  by the sensor, keyed off its own `last_success` clock — backend-agnostic.
- Doc 02's per-key ladder is re-targeted at the inner judgment cache
  (Section 5): one implementation, with doc 05 owning placement and knobs
  and doc 02 owning the write/retention rule.
- A future backend with a streaming/push model still fits: its adapter
  blocks in `judge()` until an answer is ready; the sensor's polling
  semantics are unchanged.

### (c) Action interruption

**None.** Judgment is a perception/goal-selection input; it produces no
actions and holds no execution state. Interruption cancels in-flight
*actions*; a `sense()` call in progress is a plain blocking call like any
other and is not a cancellation point. Stated explicitly so the interruption
design does not need a judgment hook. (If async judgment ever lands —
Section 8, Q1 — the async protocol would need cancellation semantics as a
required part of its design; not now.)

### (d) Confidence gating — the protocol's confidence field is gating's input

Contract (converged with doc 04; the treat-absent-as-1.0 recommendation is
withdrawn):

- Every `Answer` carries `confidence: float | None`. `None` = "backend did
  not report confidence" — never coerced to `0.0`, never silently treated
  as `1.0`.
- **Gating behavior when confidence is absent: pass-through + flagged.**
  The gating layer lets the answer through (it never invents doubt — a
  backend that cannot report confidence must still be usable) but records
  the pass-through in telemetry (`unknown_confidence_questions` /
  `confidence_absent`, per doc 04 §3.2). This preserves the distinction
  between "confident" and "unknown" that treat-as-1.0 would destroy.
- When present, confidence ∈ [0, 1] is verified (never clamped), so gating
  thresholds compare against a normalized scale without per-backend
  calibration tables. Out-of-range confidence is a contract violation:
  `JudgmentError(retryable=False)` in the generic layer, `TypeSafeError` in
  doc 04's Jev-specific extraction below the adapter boundary (translated
  at the boundary per Section 2.5).
- Jev specifics (normative for `JevJudge`): Choice and Score answers carry
  the SDK-reported confidence; Noul answers carry `None` (the SDK's
  `NoulAnswer` has no confidence field). The derived-decisiveness proxy is
  cut.
- Confidence is per-answer, not per-response: gating can distrust the
  `danger` noul while trusting the `stance` choice from the same call.

### (e) Why-diagnostics — backend identity belongs in the trace

Fields the diagnostics design should consume (canonical vocabulary —
`"jev"`, never `"typesafe"`):

- `JudgmentResponse.backend: str` — which backend answered (`"jev"`,
  `"fake"`, `"rule"`, future `"instructor-openai"`). Always present.
- `JudgmentResponse.model: str | None` — the backend's model id
  (`JevJudge` passes through the SDK response's `model`; `FakeJudge`
  reports `"fake"`).
- `JudgmentResponse.latency_ms: float | None` — measured by the adapter
  around the backend call.
- `JudgmentResponse.usage: TokenUsage | None` — typed token counts; the
  sensor copies them into the call record (no key-guessing).
- `JudgmentCallRecord` (telemetry): `source` ("sensor"/"strategy"),
  `latency_ms`, `input_tokens`/`output_tokens` (from `TokenUsage`; `None`
  when the backend doesn't report them), `error` (cause-chain-resolved
  exception class name, or `None`), `stale_cache_hit`, **`backend: str`**
  (the one additive field vs `JevCallRecord`). Doc 02/04's appended fields
  (`staleness`, gating fields) merge into this record at implementation
  time under doc 04's single ownership of the record's evolution; the
  Phase-2 translation covers the landed field set.
- `JudgmentError.backend` + `retryable` — for "why did judgment fail" traces.
- **Sensor-level hook:** `JudgmentSensor.backend_name()` and
  `JudgmentGoalStrategy.backend_name()` return the judge's `backend`
  identity (this is the hook doc 06's inspector prefers — it wraps sensors,
  not responses).
- The *questions asked* (instructions text) should be traceable too: the
  diagnostics design can log `questions` keys (names only, not full
  instructions) per call to keep traces compact; full instructions are
  available from the sensor/strategy configuration.

## 7. Test plan sketch (100% coverage gate)

New module `goapauto/models/judgment.py`, the `JevJudge` adapter, and
`goapauto/testing/fake_judge.py` must be covered at 100% with no `pragma: no
cover` in source (project rule). Test files mirror the layout:
`tests/goapauto/models/test_judgment.py`,
`tests/goapauto/models/test_jev_judge.py`.

**Core types** (`test_judgment.py`):
- Dataclass immutability (frozen) and defaults (`confidence=None`,
  `ScoreQuestion` interval defaults, `descriptions=None`, `metadata=None`).
- Construction validation: empty questions → `ValueError`; unknown
  question type → `TypeError`; bad mapping key → `ValueError`; negative
  `max_stale` → `ValueError`; duplicate/empty choices → `ValueError`;
  inverted score interval → `ValueError`; `judge=None` → `TypeError`.
- Range verification: out-of-range `FloatAnswer` value, out-of-range or NaN
  confidence → `JudgmentError(retryable=False)`; nothing is clamped
  (assert the value passes through unchanged on the raising path — i.e.,
  no silent coercion).
- `metadata` passthrough: generic layers never interpret it; the judge
  receives the exact mapping it was given.

**`FakeJudge` — first provider-independence witness** (in
`goapauto.testing.fake_judge`, alongside `fake_client.py`; it must NOT
import `typesafe_sdk` at module scope — verified by a subprocess test
asserting `typesafe_sdk` is absent from `sys.modules` after importing the
module):
- Implements `Judge` with canned `Answer` objects and a `responder`
  callable, mirroring `FakeTypeSafeClient`'s API shape (`answers=`,
  `responder=`, `model=`).
- Reports `backend="fake"`, `confidence=None` on answers by default
  (exercises the absent-confidence path), with an option to attach
  confidences.
- Can be armed to raise `JudgmentError` (outage simulation, both
  `retryable=True` and `False`) or return wrong-typed answers
  (contract-violation simulation).
- **Import trap fix:** `goapauto/testing/__init__.py` uses PEP 562 lazy
  `__getattr__` (mirroring the main package's `_JEV_ATTRS` pattern) for
  both `FakeTypeSafeClient` and `FakeJudge`, so `from goapauto.testing
  import FakeJudge` works with no `[jev]` extra installed. Both
  `__getattr__` branches are covered by tests.

**`RuleJudge` — second, non-Jev-shaped witness** (test-only, defined in
`test_judgment.py`; NOT shipped). A trivial rule-based judge with a
deliberately alien internal representation — everything is text probes and
keyword lists, no model, no SDK shapes — doing REAL translation:

- Constructor: `RuleJudge(keywords: Mapping[str, Mapping[str, list[str]]])`
  mapping question name → `{"yes": [...], "no": [...]}` keyword lists.
- `NoulQuestion` → render state as text, verdict `1.0` if any "yes" keyword
  appears else `0.0`; `confidence=None` always.
- `ChoiceQuestion` → first choice whose keyword appears in the state text;
  no match → `JudgmentError("cannot express …", backend="rule",
  retryable=False)` — the negative case: a question shape the backend
  cannot express must fail loudly, not guess.
- `ScoreQuestion` → count keyword hits, bin onto `[min_value, max_value]`
  in 5 equal bins; `confidence=None`.
- Reports `backend="rule"`. This backend cannot pass without the three
  primitives actually being translatable — a canned-answer fake cannot
  prove that.

**The key test:** the full `JudgmentSensor` + `JudgmentGoalStrategy`
suites run parametrized over THREE judges — `JevJudge(FakeTypeSafeClient
(...))` (Jev-shaped end-to-end), `FakeJudge(...)` (canned answers, no
translation), `RuleJudge(...)` (real translation from an alien
representation) — with identical behavioral assertions. If the generic
layers work unchanged against all three, provider-independence is
demonstrated, not asserted.

**`JudgmentSensor` behavior** (parametrized over all three judges):
- First `sense()` judges; unchanged observation + `min_interval=0`
  re-judges (inherited quirk — pin it); unchanged observation +
  `min_interval>0` serves cache; changed observation re-judges.
- Retryable failure with fresh cache → stale reuse + warning; retryable
  failure with dead cache (`max_stale` exceeded, incl. `max_stale=0.0`) →
  `{}` + error log.
- Non-retryable failure with `fail_loud=True` (default) → propagates
  (assert the exception identity, not just the type); with
  `fail_loud=False` → absorbed like a retryable failure.
- Non-`JudgmentError` from the judge → propagates unwrapped (fail-loud).
- `judge(observation)` path mirrors `sense()`; mixing `sense`/`judge`
  shares cache (documented, tested).
- Answer extraction per question type; mismatched answer type →
  `JudgmentError(retryable=False)` propagates (not absorbed) under the
  default policy.
- Telemetry: record fields incl. `backend`; cause-chain error naming
  (`JudgmentError` chained from `ValueError("x")` records `"ValueError"`);
  stats increment on EVERY attempted call including propagated failures;
  raising telemetry callback is swallowed.
- `backend_name()` returns the judge's backend; `close()` delegates to
  `judge.close()`; idempotent.

**`JudgmentGoalStrategy` behavior** (parametrized over all three judges):
- Empty goals → `None` without calling the backend.
- Duplicate goal labels → `ValueError`.
- Backend pick → correct goal; unknown label → `goals[0]` + warning;
  retryable `JudgmentError` → `goals[0]` + warning; non-retryable +
  `fail_loud=True` → propagates; `fail_loud=False` → `goals[0]`.
- Stats/telemetry/close delegation; `backend_name()`.

**`JevJudge` adapter** (`test_jev_judge.py`, `[jev]` extra only):
- Core→SDK question translation for all three types (incl. the canonical
  score-interval→legend derivation and its inverse mapping; descriptions→
  criteria; `metadata["jev.question"]` verbatim reuse).
- SDK→core answer extraction: Noul → `FloatAnswer(value, None)`; Choice /
  Score confidence passed through when in range; out-of-range or NaN
  confidence → `JudgmentError(retryable=False)` (assert NOT clamped);
  out-of-range noul/score → `JudgmentError(retryable=False)`.
- `TypeSafeError` subclass → `JudgmentError(backend="jev")` mapping table
  (rate-limit/timeout/connection/5xx → `retryable=True`;
  auth/4xx/validation/unclassifiable → `retryable=False`), with `from`
  chaining; missing answer key → `JudgmentError(retryable=False)` chained
  from the re-expressed `TypeSafeError("Missing answer …")`.
- Misbehaving client: a client raising raw `ValueError` (non-
  `JudgmentError`, non-`TypeSafeError`) → propagates unchanged, NOT wrapped.
- Non-`SystemOneResponse` from client → `TypeError` (inherited
  `_require_response` rule).
- Response carries `backend="jev"`, model passthrough, `TokenUsage`
  populated from SDK `Usage`, latency measurement.
- Score interval derivation: SDK `Score(criteria=[a, b, c])` →
  `(min_value=0.0, max_value=2.0)`; fresh `ScoreQuestion(0, 10)` →
  deterministic legend; inverse mapping exact (`s=1.7` on 3 rungs →
  `v` on `[min, max]` by the specified formula).

**Frozen-surface regression** (existing suites, unchanged):
- `tests/goapauto/models/test_jev.py` and `test_fake_client.py` run
  unmodified — `JevSensor`, `JevGoalStrategy`, `FakeTypeSafeClient`,
  `shared_client` behavior is pinned. Phase 1 adds no changes here at all.

**Phase-2 characterization tests** (written BEFORE delegation, run against
both implementations):
- `test_jev_sensor_delegation_question_identity`: the SDK questions
  recorded by `FakeTypeSafeClient.calls` under delegation are identical to
  the frozen path's (prompt identity via metadata round-trip).
- `test_jev_goal_strategy_delegation_prompt_identity`: the `Choice`
  question forwarded under delegation carries the original dict-valued
  criteria verbatim.
- `test_jev_sensor_delegation_error_name_preserved`:
  `records[0].error == "TypeSafeError"` under delegation (cause-chain
  rule), alongside the existing `test_sensor_records_failure*` /
  `test_strategy_records_failure` suites running unmodified.
- `test_jev_sensor_delegation_construction_failure_closes_client`: bad
  mapping under delegation raises `TypeSafeError` (translated from the
  inner `ValueError`) AND the created client is closed (no pool leak).
- Double `close()` on sensor and strategy: idempotent, no error.
- Stats on absorbed-vs-propagated failures: `_calls`/`_errors` increment
  on every attempted call in both policies.

## 8. Open questions (recorded, not guessed)

1. **Async.** Should Phase 2 add an `AsyncJudge` protocol (`async def
   judge(...)`)? The agent loop is fully sync today; async judgment only pays
   off with concurrent fan-out (multiple sensors judging in parallel) or a
   streaming backend. Defer until a concrete async backend exists — do not
   speculatively dual-surface the protocol. When it is designed, cancellation
   semantics are a required part of the design (the Phase-1 normative
   statement — a hung backend blocks the pipeline for its full timeout —
   must be superseded, not silently inherited).
2. **Confidence semantics across backends.** The protocol verifies the
   [0,1] range but does NOT guarantee cross-backend calibration: Jev's
   choice-confidence and a future logprob-derived confidence are not
   comparable quantities. The gating design must treat confidence as
   *backend-relative* (per-backend thresholds) or accept the approximation
   explicitly. Do not assume "0.8 means the same thing everywhere".
3. **Should `Judge` be `runtime_checkable`?** `GoalSelectionStrategy` is.
   Runtime-checkability only verifies method presence, not signature — useful
   for friendly error messages ("object is not a Judge: missing judge()").
   Recommend yes, for consistency — but note it can mask duck-type accidents.
   (The `backend` attribute is a documented convention, not part of the
   runtime check; the sensor defaults it to `"unknown"` when absent.)
4. **Where do future backend adapters live?** `JevJudge` lives in
   `goapauto.models.jev` because the `[jev]` extra is first-party. Future
   backends (Instructor, Outlines) each bring their own heavy dependency —
   they should live in separate distributions (`goapauto-instructor`, …),
   NOT in this repo's extras. This design does not create those packages;
   it only guarantees they *can* exist without core changes.
5. **Question instructions internationalization / prompt-injection surface.**
   `instructions` strings are free text passed to the model. No sanitization
   is specified — same as `JevSensor` today. If a future threat model needs
   it, that's a separate design, not this interface.
6. **Richer usage reporting.** `TokenUsage` covers input/output tokens —
   the only consumption unit with a cross-backend meaning today. If a
   backend needs cost, cache-hit, or per-modality accounting, extend
   `TokenUsage` with optional fields (additive, backward-compatible) rather
   than reverting to an untyped mapping.

## 9. Summary of decisions

| Decision | Choice | Why |
|---|---|---|
| Question shape | Jev's 3 primitives as core dataclasses + `metadata` escape hatch | Narrow, proven; fossilization answered by the side-channel + documented revision path, not over-abstraction |
| Answers | `FloatAnswer` (Noul+Score merged) + `ChoiceAnswer` | The two float answers were structurally identical; the question type disambiguates |
| Confidence | `Optional[float]`, `None` = not reported; never clamped, verified fail-loud | Some backends can't produce it; clamping hides bugs — validate-and-raise instead (Ruling C) |
| Absent confidence | Pass-through + flagged (doc 04's policy, normative) | Preserves confident-vs-unknown distinction; treat-as-1.0 withdrawn |
| Protocol shape | `judge(state, questions) -> JudgmentResponse`, sync | Matches agent loop; batching via the questions mapping; async deferred with the hung-backend consequence stated |
| Caching | In `JudgmentSensor`'s inner cache, above the protocol | All backends inherit it; protocol stays a pure function of (state, questions); doc 02's ladder re-targeted here |
| Errors | `JudgmentError(message, backend, retryable)`; only caught type; `retryable` precisely defined (Ruling D2) | Backend-agnostic; non-JudgmentError propagates loudly; mislabeling has consequences so the mapping is enumerated |
| Error policy | Generic: absorb retryable, propagate non-retryable (`fail_loud=True`); `fail_loud=False` reproduces frozen absorb-everything for the Phase-2 Jev delegation | Fail-loud default for new code; frozen 0.5.0 contract preserved exactly, in one documented branch |
| Construction errors | `ValueError` (never `JudgmentError`) | Fail-fast config validation must not share a type with tolerate-outage runtime failures |
| Usage | Typed `TokenUsage` dataclass | Sensor-side key-guessing on an untyped mapping silently dropped token telemetry |
| Telemetry error names | Cause-chain rule (`__cause__` class name when chained) | Most-specific failure identity; preserves frozen `"TypeSafeError"` records under delegation |
| JevSensor | Public surface frozen; Phase-2 delegation MAY proceed once characterization tests are green | No two caching implementations drifting; prompt identity via metadata round-trip; client-leak rule specified |
| Backend identity | `"jev"` (canonical); `backend_name()` sensor-level hook | Matches JevSensor/JevGoalStrategy naming; the hook doc 06's inspector prefers |
| New backends | None in Phase 1; `FakeJudge` + non-Jev-shaped `RuleJudge` prove independence in tests | Interface is the deliverable; the second witness does real translation |
| Core deps | stdlib + pydantic only | typesafe-sdk stays an optional `[jev]` extra; `FakeJudge` importable without it |

---
*New public symbols (all exported via `goapauto/__init__.py` `__all__`):
`NoulQuestion`, `ChoiceQuestion`, `ScoreQuestion`, `Question`,
`FloatAnswer`, `ChoiceAnswer`, `Answer`,
`TokenUsage`, `JudgmentResponse`, `JudgmentError`, `Judge`,
`JudgmentSensor`, `JudgmentCallRecord`, `JudgmentStats`,
`JudgmentGoalStrategy`, `FakeJudge` (via `goapauto.testing`, lazy),
`JevJudge` (via `goapauto.models.jev`, lazy like other Jev symbols).*

## 10. Adversarial review

Disposition of every numbered attack from the red-team review
(`designs/reviews/05-provider-independent-judgment-review.md`), plus the
cross-doc contradictions raised by the 02/04/06 reviews. Nothing silently
dropped. Coordinator rulings (C, D2, `"jev"` identity, escape hatch) take
precedence over review recommendations where they conflict — marked below.

1. **Contract violations: absorb vs propagate — RESOLVED.** Decided:
   the generic `JudgmentSensor` absorbs retryable errors into the
   stale-cache path and propagates non-retryable ones (fail-loud), with
   `fail_loud=False` reproducing the frozen absorb-everything policy for
   the Phase-2 `JevSensor` delegation. §3 and §4 now state the policy in
   identical words; the frozen 0.5.0 behavior is specified separately in
   §4's FROZEN row instead of contradicting the generic rule. §2.8's NOTE
   rewritten: under the default policy an escaping `JudgmentError` is
   never a transient blip.
2. **Clamp-vs-raise on out-of-range confidence — RESOLVED per Ruling C.**
   No clamping anywhere. Adapters raise `JudgmentError(retryable=False)`
   at extraction; generic layers verify ranges and raise
   `JudgmentError(retryable=False)` on violation. "The protocol guarantees
   the range" restated as "adapters validate; generic layers verify."
   Doc 04's `TypeSafeError`-on-out-of-range lives below the adapter
   boundary and is translated there — the translation direction is
   specified exactly in §2.5.
3. **Noul/Score confidence: derived decisiveness vs `None` — RESOLVED per
   Ruling C.** SDK-reported confidence when present, `None` when absent.
   The `2·|noul−0.5|` decisiveness proxy is cut (the 04 review's own
   attack 3 recommends the same cut). Grounded correction: the SDK's
   `ScoreAnswer` DOES carry confidence (required field) — only `NoulAnswer`
   lacks it — so `JevJudge` reads Score/Choice confidence and maps Noul
   to `None`.
4. **Absent-confidence policy: treat-as-1.0 vs pass-through+flagged —
   RESOLVED.** Adopted doc 04's pass-through + flagged, stated normatively
   in §6(d); the treat-absent-as-1.0 recommendation is withdrawn. (The
   review's strawman note stands: doc 04 never proposed treat-as-0.0.)
5. **Delegation acceptance criteria insufficient — RESOLVED.** The
   error-name translation rule is now exact (cause-chain rule, §2.4) and
   pinned by `test_jev_sensor_delegation_error_name_preserved` plus the
   unmodified `test_*_records_failure` suites. `JudgmentCallRecord` is
   defined as the 0.5.0 `JevCallRecord` fields + `backend`, with doc
   02/04's appended fields merging at implementation time under doc 04's
   single ownership (per the 02 review's attack-4 fix); the Phase-2
   translation covers the landed field set — the "superset" claim is no
   longer made against a moving target.
6. **Prompt drift / missing characterization tests — RESOLVED.** The
   metadata round-trip (`metadata["jev.question"]` carrying the original
   SDK question, reused verbatim by the adapter) is a no-round-trip design:
   the model prompt is byte-identical by construction, not by luck. The
   score legend derivation is FIXED (not punted): criteria list →
   `(0.0, len−1)` grounded in the SDK's "position determines its score,
   starting at zero"; fresh `ScoreQuestion` → deterministic canonical
   legend with an exact inverse mapping. New characterization tests are
   named in §7 (question identity, prompt payload, construction-failure
   chains, client-close on mid-construction failure, double close, stats
   on failure rows). Delegation is conditional on them, not "SHOULD".
7. **`retryable` worst-of-both — RESOLVED per Ruling D2 (overrides the
   "cut it" recommendation).** Kept, but precisely defined with an
   enumerated adapter mapping table (§2.5) instead of a vague boolean.
   The §2.5/§4 contradiction is gone: runtime auth failures map to
   `retryable=False` via the `TypeSafeAuthenticationError` /
   `TypeSafePermissionDeniedError` subclasses; the old "default True"
   is replaced by the D2 biconditional (unclassifiable → `False`,
   fail-loud, original chained).
8. **Non-`JudgmentError` propagation — RESOLVED per Ruling D2 (overrides
   the "wrap unknown" recommendation).** Backend-native exceptions
   propagate unchanged, documented in §2.4/§3/§4. Adapter completeness is
   still contractual: `JevJudge` MUST translate the recognized
   `TypeSafeError` set (pinned by a misbehaving-client test), and a raw
   leak propagates as the loud signal that the adapter is incomplete.
9. **Closed `Question` union as Jev lock-in — RESOLVED per coordinator
   (Jev-shaped stands + escape hatch).** The `metadata` side-channel, the
   "generic layers never interpret it" rule, and the documented fourth-
   primitive path (own question type + own `Judge` + own thin layer or a
   core revision proposal) are specified in §2.1. The `InstructorJudge`
   sketch's Noul→`bool` narrowing is fixed to a graded float field with
   an explicit anti-narrowing note.
10. **Single-fake circularity — RESOLVED.** `RuleJudge`: a second,
    non-Jev-shaped, test-only backend doing real translation (text probes
    → keyword match → parse/bin) with a loud rejection case
    (`JudgmentError(retryable=False)` when it cannot express a question).
    The suites parametrize over three judges (Jev-shaped end-to-end,
    canned-answer fake, translating rule-based fake).
11. **Sync-only + hung backend unowned — RESOLVED.** The consequence is
    now normative ("a hung judgment backend blocks the sense→plan pipeline
    for its full transport timeout; no in-flight hook exists in Phase 1"),
    the WHY-sync-is-acceptable is argued (sync loop; caching amortizes;
    per-backend transport timeouts bound the worst case), and `AsyncJudge`
    is a named Phase-2 item with cancellation semantics as a required
    part of its design.
12. **Construction error types inconsistent — RESOLVED.** All
    construction violations → `ValueError`; `JudgmentError` reserved for
    runtime backend failures. The "ValueError-equivalent" apology is
    deleted. Phase-2 boundary translation (`ValueError` →
    `TypeSafeError` in `JevSensor.__init__`) preserves the frozen
    contract.
13. **`FakeJudge` import trap — RESOLVED.** PEP 562 lazy `__getattr__` in
    `goapauto/testing/__init__.py` (mirroring `_JEV_ATTRS`); `fake_judge.py`
    has zero third-party imports at module scope, verified by a subprocess
    test. Both `__getattr__` branches covered.
14. **`usage` untyped and self-contradictory — RESOLVED.** Typed
    `TokenUsage(input_tokens, output_tokens)` dataclass; adapters populate
    it once at the boundary; the sensor copies fields (no key-guessing).
    "Interpretation is the callback's job" deleted for token counts.
15. **Phantom `NoulQuestion` comment — RESOLVED.** Deleted. There is no
    kind discriminator; the dataclass type IS the discriminator.
16. **Over-engineering — PARTIALLY ADOPTED.** Adopted: `NoulAnswer` +
    `ScoreAnswer` merged into `FloatAnswer` (structurally identical; the
    question disambiguates). Adopted: `retryable` kept but only because
    Ruling D2 mandates it — precisely defined instead of cut. Not adopted:
    hiding the `Question`/`Answer` union aliases from `__all__` (users need
    them to annotate `Mapping[str, Question]`); changing
    `descriptions` to `Mapping[str, Any]` (the Jev path's dict-valued
    criteria travel in `metadata`, keeping the core type honest).

**Cross-doc dispositions (02/04/06 reviews' sections on doc 05 —
contradictions ruled, implemented here):**

- **04, clamp-vs-raise / decisiveness / absent-policy:** ruled by Ruling C
  and implemented in §2.2/§2.5/§6(d) as above. Doc 04's designer owns the
  matching edits to doc 04's test plan (NaN/1.5 → `TypeSafeError` stays
  below the boundary; the generic path raises `JudgmentError`).
- **04, error-type split:** the same validation cannot raise both
  `TypeSafeError` and `JudgmentError` — resolved by boundary: below
  `JevJudge` → `TypeSafeError`; at and above → `JudgmentError`,
  translated per §2.5. Doc 04's Jev-specific gating keeps its
  `TypeSafeError`; the generic confidence contract (§6d) has no generic
  gating consumer yet — noted as unowned work for whoever designs generic
  gating.
- **02, protocol shape:** ruled — doc 02's `judge(observation) -> dict`
  sketch yields to `judge(state, questions) -> JudgmentResponse`; the
  boundary diagram (§5) shows the composition.
- **02, granularity (per-key ladder vs whole-cache):** 05 signs off on the
  re-target — doc 02's ladder becomes the retention/write rule OF the
  inner judgment cache (one implementation); doc 05 owns placement and
  knobs, doc 02 owns the rule and must preserve the frozen observable
  Jev contract when it lands.
- **02, async friction:** agreed — 05 defers `AsyncJudge`; doc 02's
  `AsyncSensor` ships with no judgment producer, which is doc 02's
  hazard to resolve (its review's attack 11).
- **06, vocabulary:** ruled — canonical `"jev"` everywhere; doc 06's
  `"typesafe"` hardcode and module-qualified fallback are superseded.
- **06, discovery:** resolved — `JudgmentSensor.backend_name()` /
  `JudgmentGoalStrategy.backend_name()` provide the sensor-level hook doc
  06's inspector prefers.
- **01, in-flight gap:** acknowledged — §6(a) states honestly that
  `latency_ms` is post-call attribution only and doc 01 must not assume a
  hook exists.

## 11. Producer contract

Binding contract for implementers and for the diagnostics design (doc 06).
Normative: MUST/SHALL language below is not aspirational.

**Backend identity.**
- The canonical identifier for the TypeSafe backend is `"jev"`.
  `JevJudge.backend == "jev"` (class-level constant). Every
  `JudgmentResponse` produced by `JevJudge` carries `backend="jev"`.
- `Judge` implementations SHOULD expose `backend: str` (class or instance
  attribute). `JudgmentSensor.backend_name()` and
  `JudgmentGoalStrategy.backend_name()` return `self._judge.backend`, or
  `"unknown"` if the attribute is absent. Diagnostics MUST use
  `backend_name()` for sensor-level identity and `JudgmentResponse.backend`
  for call-level identity; the two are equal for a correctly implemented
  backend.

**`JudgmentError` fields.**
- Constructor: `JudgmentError(message: str, *, backend: str,
  retryable: bool)`. `retryable` is REQUIRED — no default; every raise
  site decides.
- `retryable=True` IFF transient: rate-limit/429, network timeout,
  connection error, 5xx/service-unavailable.
- `retryable=False` IFF permanent for this call: auth failure
  (invalid/revoked/missing key; 401/403), malformed question (validation
  failure before any backend call), contract violation (missing answer
  key, wrong answer type, out-of-range value/confidence).
- Generic layers catch ONLY `JudgmentError`. Any other exception type
  raised by a `Judge` propagates unchanged (Ruling D2) — adapters SHOULD
  chain the original (`from exc`); generic layers MUST NOT wrap.
- Telemetry error naming: `type(exc.__cause__).__name__` when the
  `JudgmentError` chains a cause, else `type(exc).__name__`. Deterministic
  and backend-agnostic.

**Translation rules at the `JevJudge` boundary (exact, both directions).**
- INBOUND (SDK → core): any `TypeSafeError` escaping the client call or
  SDK response handling is caught inside `JevJudge.judge` and re-raised
  as `JudgmentError(str(exc), backend="jev", retryable=<mapped>)` with
  `from exc`, where `<mapped>` follows the §2.5 table (rate-limit /
  timeout / connection / 5xx → True; auth / 4xx / response-validation /
  unclassifiable base → False; most-specific subclass checked first).
- `KeyError` from the SDK's typed answer accessors (missing or
  wrong-typed answer — the accessors filter by `isinstance`) is
  re-expressed as `TypeSafeError(f"Missing answer for question {name!r}")`
  and then translated per the inbound rule (→ `retryable=False`, cause
  chain preserves the `"TypeSafeError"` name).
- The adapter's OWN checks (empty questions, untranslatable question
  type, out-of-range value/confidence — never clamped) raise
  `JudgmentError(backend="jev", retryable=False)` DIRECTLY, with no
  `TypeSafeError` involved.
- NOTHING named `TypeSafeError` crosses `JevJudge.judge` outward. The
  frozen `JevSensor`/`JevGoalStrategy` keep their `except TypeSafeError`
  handling untouched in Phase 1; under Phase-2 delegation their
  `TypeSafeError`s arrive via the cause chain of the translated
  `JudgmentError`, and the outer layers re-raise/translate at their own
  boundaries per §2.6 criteria #2–#3.
- Non-`TypeSafeError`, non-`JudgmentError` exceptions from the client
  propagate unchanged (miswired client, raw transport leak — loud by
  design). `_require_response`'s `TypeError` propagates unchanged.

**Answer value contract (what downstream layers may assume without
re-validating).**
- `NoulQuestion` → `FloatAnswer`, `value` ∈ [0, 1].
- `ScoreQuestion` → `FloatAnswer`, `value` ∈ [`min_value`, `max_value`].
- `ChoiceQuestion` → `ChoiceAnswer`, `value` ∈ the asked `choices`.
- `confidence` ∈ [0, 1] whenever not `None`; `None` means "backend did
  not report confidence" — never coerced, never clamped.
- Violations are `JudgmentError(retryable=False)` raised fail-loud at the
  point of detection (adapter extraction, then generic-layer
  verification).

**What the diagnostics doc (06) may consume.**
- `JudgmentResponse.backend: str` (always present), `.model: str | None`,
  `.latency_ms: float | None`, `.usage: TokenUsage | None`
  (`.input_tokens` / `.output_tokens`, each `int | None`).
- `JudgmentSensor.backend_name()` / `JudgmentGoalStrategy.backend_name()`
  → `str` (sensor-level identity; `"unknown"` fallback).
- `JudgmentCallRecord`: `source`, `latency_ms`, `input_tokens`,
  `output_tokens`, `error` (cause-chain-resolved class name or `None`),
  `stale_cache_hit`, `backend`. (Doc 02/04's appended fields merge here at
  implementation time under doc 04's single ownership; the record stays
  additive and backward-compatible.)
- `JudgmentError.backend: str`, `.retryable: bool` — for "why did
  judgment fail" traces.
- Question names per call (keys of the asked mapping) for compact traces;
  full instructions available from sensor/strategy configuration.
- The diagnostics design MUST NOT assume: cross-backend confidence
  calibration (open question §8.2), in-flight observability of `judge()`
  (none exists in Phase 1), or any spelling of the backend name other
  than the canonical `backend` strings.
