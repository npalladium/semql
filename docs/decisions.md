# Design decisions

Pinned answers to recurring "should we add X?" questions. Each entry
records the *reason* for the call so a future revisit can weigh
whether the constraints have changed.

The format is loosely an ADR — context, decision, consequences —
condensed to a paragraph each.

---

## D1. PyYAML — no, not in core

**Context.** Some BI tooling (Cube.js, dbt, LookML) loads cube
definitions from YAML files. The question recurs: should `semql`
ship a YAML loader, or expose one through an optional extra
(`pip install semql[yaml]`)?

**Decision.** Neither. Python is the native catalog language. A
YAML loader sits outside core and outside extras — if and when
demand is real, it ships as a separate package (`semql-yaml`).

**Why.** PHILOSOPHY.md is explicit: "Python is the native language
for cube definitions. Type safety, refactoring, and testing come
free." A YAML loader inside core invites the long tail of dynamic
loading concerns (schema validation against a remote registry,
templating, hot reload) and dilutes the type-checker payoff. The
out-of-tree shape preserves "dependencies you don't need should
cost nothing to avoid."

**Revisit.** If three independent users ship YAML loaders to bridge
non-Python services, and the loaders converge on a roughly common
schema, accept it as `semql-yaml`.

---

## D2. mypy and pyright — keep both, for now

**Context.** Both run under `just typecheck`. They overlap heavily;
neither subsumes the other. The dev loop runs ~1s slower than it
would with just one.

**Decision.** Keep both pre-v1. mypy stays the structural backbone
(strict mode catches the classic generics / variance bugs); pyright
catches more in narrow inference corners (Pydantic field
resolution, `runtime_checkable` Protocol drift). Together they have
caught real bugs that one alone missed during this codebase's
build-out — the cost is worth the redundancy.

**Why.** Pre-v1 is when type discipline pays the most: the surface
is moving, and a single bad inference can ripple into the public
API. Once the surface freezes at v1, the marginal value of the
second checker drops and we pick one.

**Revisit.** At v1 cut. Drop pyright if mypy's gaps haven't bitten
in the prior six months.

---

## D3. Fluent Interface on top of Pydantic — no

**Context.** Could we layer a builder-style `Cube.named("orders")
.with_table("orders").with_measure(...)` API on top of the
Pydantic constructor? Some users prefer chained-call ergonomics.

**Decision.** No. Pydantic kwargs are the only catalog-authoring
API. There is no fluent layer, no shorthand factory, no DSL.

**Why.** Two reasons. First, kwarg construction with type hints +
default values is already short — every "extra" character is a
catch by the type checker. Second, a fluent layer is a parallel
public surface that has to be kept in sync with the data model on
every change; doubling the maintenance cost of every new field is
not worth the cosmetic gain. PHILOSOPHY.md: "Composes with your
stack — it does not own it."

**Revisit.** If a downstream tool needs a callable / chainable
shape (e.g. a no-code UI builder), they layer it themselves on top
of `Cube(...)`. Don't pull it into core.

---

## D4. Catalog value types are frozen Pydantic models

**Context.** Should `Measure`, `Dimension`, `Cube`, `SemanticQuery`,
etc. be frozen?

**Decision.** Frozen. `model_config = ConfigDict(frozen=True)`
everywhere except `Cube` itself (which has too many fields and
internal cross-validation to lock down right now).

**Why.** Catalog and spec objects are value types in the
Evans/Fowler sense — equality is structural, identity is
irrelevant, and mutation invites consistency bugs the compiler
can't catch (rebinding `query.measures` after compile started would
produce a result that disagrees with the query the caller built).
Frozen is the default for value objects.

**Revisit.** Only if a real performance scenario emerges where
copy-on-modify dominates. The Pydantic v2 `model_copy(update=...)`
shape covers the common "tweak one field" need without re-opening
mutation.

---

## D5. `having` stays; `Measure.filter` is a different feature

**Context.** The ktx port plan locked "No HAVING — aggregates only
via `Measure.filter`", but `SemanticQuery.having` already exists on
main: exported, federated (`MergeSpec.having`), mapped by the SQL
parser, property-tested. The two looked like substitutes.

**Decision.** Keep both, with distinct jobs. `having` is the
*query-time* post-aggregate predicate — the LLM invents the
threshold per question ("groups where `sum(revenue) > 1000`").
`Measure.filter` (lands with the chasm-trap milestone) is
*catalog-time* conditional aggregation (`SUM(CASE WHEN approved …)`),
authored once by the catalog owner. The locality emission path is
scoped accordingly: no WHERE/HAVING auto-classifier; `Measure.filter`
applies inside per-group CTEs, `having` at the outer SELECT.

**Why.** An LLM cannot express an ad-hoc threshold through a catalog
field — removing `having` would delete real query-time
expressiveness, not consolidate it. The original "No HAVING" decision
is read as a constraint on the locality path's internal
classification, not as a directive to remove the public field.
(Maintainer-confirmed 2026-06-12; see
`docs/specs/roadmap-reconciliation-2026-06.md` §R1.)

**Revisit.** If `Measure.filter` plus `InlineDerived` turn out to
cover every observed `having` use in practice, re-open the
consolidation question before the v1 freeze — not after.

---

## D6. Drop `PolarsMergeEngine` — one merge implementation, not two

**Context.** Review defect A3: `PolarsMergeEngine` hand-reimplements
the merge in Polars and had drifted from the canonical DuckDB merge
SQL — it silently dropped cross-partition filter literals whose op it
didn't recognise (`in`/`not_in`/`is_null`/`not_null`/`contains`) and
read only `vals[0]`, so the *same* `FederatedPlan` returned different
rows depending on the merge engine. The fix was small, but the
question A3 actually poses is whether a parallel merge implementation
should exist at all.

**Decision.** Remove `PolarsMergeEngine` (engine, `merge`
subpackage, and contract tests — ~775 LoC). The DuckDB merge SQL
(`FederatedPlan.merge.sql`) is the single source of truth. The
generic `MergeEngine` / `AsyncMergeEngine` plug-in protocol stays —
a caller can still register a custom merge engine — but the project
ships exactly one implementation.

**Why.** Its stated reason to exist — "merge without a DuckDB
dependency at execute time" — was already false: `semql-engine`
hard-depends on `duckdb` (`engine.py:35`) and `Engine.__init__`
opens a `duckdb.connect(":memory:")` regardless of the merge engine.
`polars` was never declared in any `pyproject` (a phantom dep) and
the advertised `semql-engine[polars]` extra didn't exist. So the
engine bought no dependency reduction while adding a second
hand-written copy of the merge semantics — the exact divergence
hazard A3 is, and structurally the same debt as B1's parallel
federate compiler. Deleting it makes the A3 *class* of bug
impossible rather than merely fixing this instance.

**Revisit.** If a DataFrame-native merge runtime becomes a real
requirement (e.g. an Arrow/Polars-only deployment that genuinely
drops DuckDB), reintroduce it behind the existing `MergeEngine`
protocol — but gate it on a differential harness that runs every
`FederatedPlan` through both engines and asserts identical rows, so
it can never silently diverge again. (Maintainer-confirmed
2026-06-12.)

---

## D7. Time-window ranges are half-open and compared by instant

**Context.** Review defect A2. `TimeWindow.range`'s docstring said
"Inclusive (start, end)", but the compiler emits `dim >= start AND
dim < end` — a half-open `[start, end)` window — and the
time-partition router (`_ranges_intersect`) compared endpoint
*strings* lexically. The two questions A2 forces: is the window
inclusive or half-open, and how are endpoints ordered?

**Decision.** Half-open `[start, end)` is canonical — the emitted SQL
is the source of truth and already half-open; the docstring was the
bug and was corrected. Range endpoints are compared by *instant*, not
text: a shared `spec.parse_instant` parses each ISO-8601 endpoint to
an aware `datetime`, and the router and the `TimePartitionedSource`
range-ordering validator both compare the parsed values. Naive
(offset-less) timestamps are read as **UTC** so a naive endpoint stays
comparable with an offset-bearing one.

**Why.** Lexical comparison only *coincidentally* matches chronological
order — for zero-padded, same-offset ISO-8601. The instant two
endpoints differ in UTC offset (or precision), byte order diverges
from instant order: a query window in `-05:00` whose rows all fall in
the post-boundary physical source was routed to the *pre*-boundary
table and silently returned empty. Comparing instants is the only
comparison that matches what the `>= / <` filter actually selects.
Half-open is also what the rest of the model already assumes
(`TimePartitionedSource` docstring) and what review item B9 recommends
standardising on everywhere.

**Revisit.** The naive-is-UTC reading is a pragmatic default, not a
timezone model. When per-cube/per-dimension timezone semantics land
(B9), `parse_instant`'s default should defer to the declared zone, and
endpoints should be parsed at construction (so a malformed or
ambiguous timestamp is refused when the cube/query is built, not at
route time). (Maintainer-confirmed 2026-06-12.)

---

## D8. Cross-cube type coercion is refused, with `Dimension.coerce_to` opt-in

**Context.** Review item I10 (promoted to the W1 correctness tier by R6).
A federated bridge join equates two cubes' keys with a bare `a.k = b.k`;
when the keys' declared `Dimension.type` differed (a `uuid` order key vs
a `string` customer id), the merge engine coerced one side silently,
which can drop or invent matches. That's a refusal-over-omission
violation. The question: refuse, coerce, or warn — and where.

**Decision.** Refuse at compile time with `FederationError(reason=
"cross_cube_type_coercion")`. The escape hatch is `Dimension.coerce_to:
DimTypeLiteral | None` — a dimension declares the *additional* type it
is willing to be compared as. A join is allowed when the two keys share
at least one acceptable type, where a key's acceptable set is
`{type} ∪ {coerce_to}`. `coerce_to == type` is itself a construction
error (it coerces nothing). The opt-in is rendered in the planner
prompt next to the dimension's `type`.

**Scope.** The refusal covers the **federated bridge path only**
(`federate._parse_bridge`), where SemQL holds the join keys as
structured, typed dimensions. Same-backend joins specify their key in a
raw-SQL `on` clause whose column types SemQL cannot see — that's the
raw-SQL escape hatch (B2), and it stays uncovered until an expression
IR exists. The check sits in `_parse_bridge`, the single funnel both the
distributive and raw_rows merge paths route through, so neither can
emit a coercing join.

**Why.** "Wrong results are the only unacceptable outcome." A silent
type coercion in a join key is precisely a wrong-rows generator, and
unlike a missing label it's invisible in the output. Making the catalog
author write `coerce_to` turns an accident into a decision. Type
mismatch isn't representable for every case yet — there's no date-vs-
timestamp distinction in `DimTypeLiteral` (B9) — so I10 catches the
mismatches the type system can currently express (uuid/string,
number/string, …) and grows as the type vocabulary does.

**Revisit.** When same-backend joins gain a structured key
representation (B2/B3 — QualifiedRef + expression IR), extend the same
check to them. When the temporal model splits date from timestamp (B9),
those become catchable mismatches too. (Maintainer-confirmed
2026-06-12.)

---

## D9. Honouring `Join.kind` at emission waits for the join-graph rebuild (W3)

**Context.** W2 (review B1) listed "`Join.kind` honoured" — the emitter
hardcodes `join_type="left"` (`compile.py`) while the plan carries a
`kind` (`logical.py` sets `left` for cubes in `query.left_joins`, else
`inner`, matching the spec doc "Cubes to LEFT JOIN instead of INNER").
The naive fix is to pass `plan_join.kind` to the emitter.

**Decision.** Defer it to W3 (the ktx M2 join-graph rebuild).
`build_join_graph` does not yet compute the correct FROM root /
direction for the `left_joins` *spine* case: a query with
`left_joins=["facts"]` and a dimension from the spine cube roots at
`facts` and emits `facts → spine`, so the spine cube lands on the
`right` with `kind="inner"`. Honouring that `kind` produces a
wrong-rooted INNER join that drops the very rows the spine feature
exists to keep (e.g. employees with zero punches). The plan's `kind` is
only as correct as the graph that assigns it, and root/direction
selection is exactly what W3 rebuilds (Dijkstra + `JoinPath`).

**Why.** Honouring a provably-incorrect `kind` is not an improvement
over the (also-wrong-but-stable) always-LEFT status quo — it trades one
wrong result for another and ships a regression for spine queries.
Fixing it properly means correcting root/direction in the join graph,
which would mean building on `build_join_graph` right before W3 replaces
it (the anti-pattern R3 warns against). The emitter is left at
always-LEFT until the graph produces a trustworthy `kind`.

**Revisit.** With W3's join-graph rebuild — at which point the emitter
reads `plan_join.kind` and a spine query roots at the spine cube with
the fact cube LEFT-joined. (Maintainer-confirmed 2026-06-12.)

---

## D10. The federation parallel-compiler deletion is deferred (post-W2)

**Context.** W2 (review B1) listed "replace federate.py's ~700-LoC
parallel compiler with a `LogicalPlan` split-point feeding the shared
emitter (killing `_lit` literal inlining)". The split-point primitive
exists (`logical.partition_scans`) and `compile_plan` now trusts a
prebuilt plan, so the load-bearing prerequisite is in place. But the
rewrite itself was deferred.

**Decision.** Treat W2 as functionally complete with the parallel
compiler still in place. The behaviour-affecting W2 goals all landed and
are green: the emitter trusts the plan (`compile_plan` no longer
re-plans — a rewritten scan / pushed-down predicate survives to
emission), one alias convention (`output_alias`), `CompareSplit` is
load-bearing, B6 keys predicate resolution by dimension, the
distributive path lifts the where-tree + segments (the R3 carryovers /
5 parked A4 tests), and `FederatedPlan` is frozen + version-stamped.
`Join.kind` is the only IR-adoption item parked, and that's D9 (W3).

**Why.** Deleting the parallel compiler changes *no behaviour* — every
federation test already passes through it — so it is regression risk
with no user-visible upside, and it is large: `partition_scans` today
gives each backend only its scans/joins; the rewrite must move filter /
segment routing, bridge-key projection, avg-decomposition, measure
routing, and a merge-spec derivation into the plan layer. That is a
multi-step effort of its own, best built incrementally and verified
against the existing federation suite as an oracle, not folded into
W2's tail. Shipping the correctness wins now and doing the refactor
deliberately later is the lower-risk sequencing.

**Revisit.** As its own workstream: extend `partition_scans` to a full
predicate router + bridge-projection injector, derive the merge from
partitioned plans, replace `_lit` inlining with bound params, then
delete `_build_partition_sub_query` / `_emit_merge_sql` and the
raw_rows twins. The existing federation tests are the behaviour oracle —
they must stay byte-stable through the swap. (Maintainer-confirmed
2026-06-13.)
