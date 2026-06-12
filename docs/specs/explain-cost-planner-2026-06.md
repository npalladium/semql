# Explain, cost, and planning — improvement plan (2026-06)

Status: proposal. Companion to `architecture-review-2026-06.md`,
`naming-convention.md`, and `roadmap-reconciliation-2026-06.md` (W1–W8
sequence). The LLM/MCP-facing consequences live in the sibling doc
`llm-interface-2026-06.md`.

---

## 0. Current state, measured

Three facts anchor everything below.

1. **"Explain" means two different things.** `Catalog.explain()`
   (`catalog.py:405`) returns `repr(LogicalPlan)` — an unstable Python
   repr, useful to a human at a REPL, useless to a machine, and not a
   contract anything can test against. The MCP `explain` tool
   (`semql_mcp/server.py:145`) returns a bare SQL string, and on
   compile failure returns `"-- compile failed: {exc}"` — an error
   masquerading as success.

2. **Cost sums where it should multiply.** `estimate_cost`
   (`cost.py:120`) walks the query's touched cubes and **sums** their
   `size_hint`s. A join does not scan `orders + customers` rows; it
   scans `driver × fan-out`. The model has no join multiplier, no
   selectivity, no partition awareness, and `rows_scanned` conflates
   scanned/returned/bytes. The docstring is honest that it's rough —
   but it is rough in the wrong *shape*, not just the wrong magnitude.

3. **There is no planner.** Compilation is deterministic lowering.
   The decisions that *are* made — rollup routing, join-kind
   inference, federation split-point — are rule-based, invisible to
   the caller, and consume no cost information.

---

## 1. Explain: from string to structured report

### 1.1 `ExplainReport`

One type replaces both current explains. Per `naming-convention.md`
§1.4 this is a process-boundary type: frozen Pydantic, versioned.

```python
class ExplainReport(BaseModel):     # frozen=True
    version: int = 1
    plan: PlanSummary               # serialized LogicalPlan, NOT repr()
    sql: dict[str, str]             # per-backend rendered SQL
    decisions: tuple[PlanDecision, ...]
    cost: CostEstimate
    auth: tuple[AuthInjection, ...] # security_sql / scope predicates, by cube
```

### 1.2 `decisions` — the compiler narrates its inferences

Every silent inference becomes a `PlanDecision` record:

- rollup chosen (and why each non-chosen candidate failed to match)
- join kind inferred; `left_joins` applied
- partition pruning result (range kept / dropped)
- having-vs-where placement
- federation split-point and merge strategy
- CNF / pushdown transforms applied (post-B1)

This is the naming grammar's "*Decision = surfaced inference" applied
to the compiler itself. It is also a defect detector: a filter that
arrives in the query but produces **no decision and no predicate** is
exactly the A1 failure mode (compile_plan dropping filters) — and with
a decision trace, that becomes a testable invariant rather than a
silent omission. Pin it: *every query input either appears in the
plan, appears in a decision, or raises.* (Refusal over omission.)

### 1.3 Render levels

Model on `EXPLAIN` / `EXPLAIN ANALYZE` / `FORMAT JSON`:

| level     | contents                                   | cost to produce      |
|-----------|--------------------------------------------|----------------------|
| `plan`    | PlanSummary + decisions + static cost      | compile-front only   |
| `sql`     | plan + rendered SQL per backend            | full compile         |
| `analyze` | sql + backend-native EXPLAIN / dry-run     | engine round-trip    |

`analyze` lives in `semql-engine` (it needs an adapter); `plan` and
`sql` live in core. `Catalog.explain()` keeps its signature but gains
`level=` and returns `ExplainReport`; a `.render()` gives the human
string the repr used to approximate.

### 1.4 Explain doesn't lie — executable invariant

`ExplainReport.plan` must be the plan `compile_plan` actually emits
from. Once B1 lands this is nearly free: it is the P9 path-equivalence
property (`property-testing.md`) applied at the explain boundary. Add
a differential test now, even pre-B1, so the unification can't drift.

---

## 2. Cost: fix the shape, then add tiers

In leverage order.

### 2.1 Consume the join path, not the touched-cube set

`rows ≈ root_size × Π(fanout per one_to_many edge)`. ktx M2's
`JoinPath{has_one_to_many}` metadata supplies the per-edge fan-out
flags; the cost model and the Dijkstra edge weights (one_to_many × 10)
must share one fan-out model with two consumers. **Sequencing:** rides
on B1/W2 per the R2 decision — do not build this on the parallel
federate compiler.

### 2.2 Backend estimates as a declared capability

```python
class Adapter(Protocol):
    def estimate(self, compiled: CompiledQuery) -> BackendEstimate | None: ...
```

Optional per adapter, *declared not assumed* (philosophy):

- **BigQuery**: dry-run → exact bytes scanned, zero cost. The killer
  feature; ship first.
- **ClickHouse**: `EXPLAIN ESTIMATE` → rows/marks/parts.
- **Postgres**: `EXPLAIN (FORMAT JSON)` → planner row estimates.
- Adapters that can't estimate return `None`; they do not fake it.

`CostEstimate` then carries two tiers: `static` (catalog heuristic,
always available, compile-time) and `backend` (live, opt-in,
engine-time). `ExplainReport` level `analyze` populates the latter.

### 2.3 Cheap selectivity from data we already hold

- Time window over a partitioned source → fraction of total range
  (`partition.py` already owns the boundary math; D-pinned half-open).
- Equality filter on a `Lookup`-backed dimension → `1 / |values|`.
- Keep **three axes**: `rows_scanned`, `rows_returned`,
  `bytes_scanned`. `LIMIT` caps returned, not scanned; BigQuery bills
  bytes, not rows. Today's single `total_rows_scanned` conflates them.

### 2.4 Calibration, not tuning

The P7 `on_execute` hook already observes actual rows and duration.
Record `(estimate, actual)` pairs; expose an accuracy report
(per-cube error distribution). Do **not** auto-tune yet — measure
first, decide later whether tuning earns its complexity.

### 2.5 `size_hint` freshness

`size_hint` is a hand-written constant; it rots. Add
`semql-introspect refresh-hints`: pull table stats (or `count(*)`)
and write a sidecar file the catalog can layer in — never edit
catalog code. A stale-hint warning (hint age > N days, if the sidecar
records timestamps) keeps the estimate honest.

### 2.6 Resolve the unknown-bypass philosophy conflict

`QueryBudget.check` treats `rows_scanned_unknown=True` as a free pass
(`cost.py:96`). That contradicts *refusal over omission*. Make it a
knob:

```python
class QueryBudget(BaseModel):
    on_unknown: Literal["allow", "refuse"] = "allow"
```

Default stays `allow` (don't break the current contract); a paranoid
deployment can refuse. **Record as decisions.md D6** when implemented.

---

## 3. Planner: deliberately, there isn't one

Position: do **not** build a cost-based optimizer. The backend
database already does cost-based optimization with real statistics; a
semantic layer that second-guesses it loses on both correctness and
maintenance. SemQL's job is to emit good SQL and refuse bad queries.

The planning decisions worth owning are exactly three, and all become
*cost consumers* rather than a new component:

1. **Cost-aware rollup routing.** Today routing is purely rule-based
   ("rollup covers the query → use it"). Give rollups `size_hint`s
   and route only when covered **and** estimated cost is lower. The
   decision (either way) lands in `ExplainReport.decisions`.
2. **Join-path selection.** ktx M2's weighted Dijkstra *is* the
   planner improvement; already sequenced (W3, after B1). No new work
   beyond sharing the fan-out model with §2.1.
3. **Federation merge guard.** Estimate fragment result sizes before
   pulling rows into the Polars merge; a fragment estimated at 100M
   rows client-side is a budget refusal, not a silent haul.

All three are plan-level concerns and land after B1, consistent with
the R2/R3 decisions. Nothing here justifies a `planner.py`.

---

## 4. Sequencing against the reconciliation workstreams

| item                                    | depends on | slot                |
|-----------------------------------------|------------|---------------------|
| §2.6 `on_unknown` knob (+ D6)           | nothing    | Week 1 (with W1)    |
| §1.4 explain≡compile differential test  | nothing    | Week 1 (with W5)    |
| §2.5 `refresh-hints`                    | nothing    | W6 (DX), anytime    |
| §1.1–1.3 `ExplainReport` + decisions    | B1 helps*  | W2 tail             |
| §2.2 `Adapter.estimate` + BQ dry-run    | nothing    | W7-adjacent (engine)|
| §2.1 join-path cost model               | B1 + ktx M2| W3                  |
| §2.3 selectivity heuristics             | §2.1       | W3 tail             |
| §2.4 calibration recording              | P7 (done)  | W7                  |
| §3.1 cost-aware rollup routing          | §1, §2.1   | post-W3             |
| §3.3 federation merge guard             | §2.1, W2   | post-W2             |

\* `ExplainReport` *can* ship pre-B1 over the current plan, but the
decision-trace vocabulary stabilises with the IR; building it twice is
waste. Recommendation: scaffold the type + the two trivially-true
decisions (rollup, join-kind) pre-B1 only if the MCP work (sibling
doc) needs the envelope sooner.
