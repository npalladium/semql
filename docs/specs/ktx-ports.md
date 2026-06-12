# Plan: Cleaner-and-better ports from ktx

## Scope

Implementation plan for the 17 candidates in
`docs/specs/ktx-borrowed.md` (12 borrowed from ktx, 4 added on the
depth pass, 1 architectural lint). One design constraint runs through
all of them: **every port has to look like it was written for
SemQL, not translated from ktx.** Concrete consequences:

- Frozen Pydantic everywhere, no `model_copy(update=...)`
  mutate-then-return idiom.
- New types live in `model.py` / `plan.py` / `spec.py`, not
  scattered.
- New compile paths go in `compile.py` alongside
  `_emit_simple_query` and `_emit_compare_query`.
- New value types compose with `CompiledQuery`; the existing
  public surface is preserved.
- Every new test uses syrupy snapshots, mirroring
  `test_plan_snapshots.py` and `test_snapshots.py`.

## Locked decisions

Resolved before planning began, surfaced via `AskUserQuestion`:

1. **No HAVING.** User filters reference dimensions and
   segments; aggregates only via `Measure.filter` (candidate
   13's `_apply_measure_filter`). Cleaner spec, more catalog
   authorship, no WHERE/HAVING split to test.
2. **Refactor ktx's `_detect_fan_out` first, port second.**
   The 250-LoC ktx function becomes three independently
   testable functions in SemQL. Adds ~1 day of refactoring
   before the chasm-trap work begins.
3. **Reject nested WITH** in `DerivedTable.sql`. Force the
   explicit `with_ctes` mechanism. Hoisting (ktx's approach)
   is not on the table.
4. **Plan all 17 candidates** as a single roadmap, broken into
   milestones at execution time.

## Milestones

Six milestones, each independently shippable, ordered to
minimise risk on the most valuable work.

### M1 — Cheap-wins batch (candidates 3, 4, 7, 8, 9, 12, 13)

One PR, ~1 day. All candidates < 50 LoC, no architectural
risk. **Note: candidate 17 (InlineDerived Phase A lift) was
originally in M1 but is moved to M2 — see R4 in Risks.**

- **C3 — `Provenance` enum.** Add
  `Literal["verified", "composed", "dimension"]` to
  `BaseField` (`model.py:109`). Catalog carries provenance;
  the plan carries it through. Presenter/Drilldown roles in
  the prompt pipeline (`prompt.py`) read it from the plan
  directly, no extra lookup.
- **C4 — `ValidationReport` (warnings channel).** Add
  `warnings: tuple[str, ...]` to `CompiledQuery`
  (`compile.py:175`). The new path is via a
  `compile_query_with_warnings()` companion or an opt-in
  `strict: bool = False` flag on `compile_query`. Errors stay
  as exceptions (SemQL convention).
- **C7 — Reserved-identifier quoting.** Extend
  `_parse_fragment` (`compile.py:691`) to call a new
  `quote_reserved_identifiers(expr: str) -> str` helper
  before `sqlglot.parse_one`. Single point of insertion; the
  rest of the compiler pipeline doesn't change. Add syrupy
  fixtures for `group.key`, `order.status`, and
  `'group.value'`-as-string-literal cases.
- **C8 — DIALECT CONVENTION blocks.** Add a 5-line comment
  block at the top of `compile.py`, `dialect.py`,
  `backend.py`, `parse.py`, `plan.py`. Per-milestone
  AGENTS.md style.
- **C9 — `lru_cache` on parse.** Wrap `_parse_fragment` with
  `functools.lru_cache(maxsize=256)` keyed on
  `(sql, dialect)`. One decorator.
- **C12 — `_inside_subquery` aggregate detection.** Add a
  helper `classify_aggregate(expr: exp.Expression) -> bool`
  to `parse.py` (the SQL→SemanticQuery direction) that walks
  the AST and returns `False` if the only aggregate is inside
  a subquery. **Conditional on whether SemQL currently
  rejects subqueries in WHERE filters.** Verify first; if
  SemQL already rejects (likely — check `validate.py`), C12
  is moot and we drop it from M1.
- **C13 — `_apply_measure_filter`.** New pure function in
  `dialect.py` (or a new `measure.py` if `dialect.py` is
  already heavy):
  `inject_measure_filter(agg_expr: exp.Expression, filter_expr: exp.Expression) -> exp.Expression`.
  Walk AST; for each `exp.AggFunc`, replace `.this` with
  `CASE WHEN filter THEN inner END`. Handle `count(*)` and
  `count(DISTINCT x)` as special cases (ktx:684-708). Wires
  up to `Measure.filter` once that field is added — see R3
  in Risks. The helper ships in M1 even though no caller
  exists yet; the call site lands in M3.

Tests: ~15-20 new tests, all syrupy snapshots. New snapshot
corpus: `__snapshots__/test_ktx_borrowed_*.ambr`. Run
`just check` until green.

### M2 — Structural value types + graph upgrade (candidates 6, 14, 17)

~3-4 days. Required by M3 (chasm-trap work), but each is
independently valuable.

- **C6 — Steiner-tree + Dijkstra + ambiguity.** Replace
  `logical.py:find_join_path` (line 447) with a Dijkstra
  implementation. New return type `JoinPath` (frozen
  dataclass or Pydantic):
  `edges: tuple[tuple[Cube, Cube, ModelJoin], ...], is_ambiguous: bool, has_one_to_many: bool`.
  The `is_ambiguous` flag threads through `build_join_graph`
  → `ResolvedPlan` → a `ValidationReport` warning (C4).
  Cost weighting: 1× for `many_to_one` / `one_to_one`, 10×
  for `one_to_many`. ~60 LoC of new graph algorithm; the
  public surface change is the `is_ambiguous` field, which
  is additive.
- **C14 — `MeasureGroup` value type.** New in `plan.py`:
  `MeasureGroup(cube_name: str, measures: tuple[ResolvedMeasure, ...], join_path_to_dims: tuple[ResolvedJoin, ...])`.
  Computed from `Cube.grain` + `Cube.joins` at plan time,
  not from re-walking the graph. The per-group join path is
  determined by `find_path(cube_name, dim_cube_name)` (now
  using the upgraded Dijkstra from C6). ~50 LoC.
- **C17 — Lift Phase A restriction on `InlineDerived`** (was
  in M1; moved here per R4). Add
  `dependencies: tuple[str, ...]` field to `InlineDerived`
  (`spec.py:171`). Compile-time resolver in `compile.py`
  walks the operand graph and accepts cross-cube refs with
  a clear error if the join graph can't safely reach them.
  ~80 LoC of compile-side work; the value type change is
  trivial. **Lands after C6 in this milestone**, since the
  cross-cube check depends on the upgraded graph.
- **C15 — No HAVING classifier (locked decision).** This
  candidate is **dropped** in favor of the
  `_apply_measure_filter` route (C13). Filter classification
  in `compile.py:_predicate_term` stays as-is; the compiler
  never splits a filter into WHERE/HAVING. The work that
  would have gone here is instead in M3, where per-measure
  filters emit as `CASE WHEN` inside the per-group CTE.

Tests: ~10 new tests for the upgraded graph; ~5 for
`MeasureGroup` construction. Snapshots for ambiguous join
warnings (cases where ktx would log + pick; we now also
surface a warning to the caller). C17 tests: ~5 cross-cube
operand cases + 3 error cases (unreachable operand,
ambiguous operand, no join path).

### M3 — Chasm-trap detection + aggregate-locality (candidate 1)

~1-2 weeks with snapshots. The centerpiece. The
refactor-first decision means this milestone is two phases.

**Phase 3a — Refactor ktx in place (in the scratch clone, for
understanding).** Read `_detect_fan_out`
(`planner.py:932-1111`) and split it into three pure
functions *in the ktx clone* (not in SemQL yet). Verify the
ktx test suite still passes after the split. This is a
learning exercise; the resulting ktx code is throwaway. The
goal is to confirm the split works before we port.

The split:

- `_validate_multi_cube_measure_refs(measures, measure_groups)`
  — multi-source validation (ktx:952-979)
- `_detect_single_group_fanout(group, dimensions, filters, graph)`
  — single-group fanout (ktx:992-1039)
- `_partition_into_safe_measure_groups(measure_groups, dim_sources, graph)`
  — chasm trap via safe-merge (ktx:1113-1190, with the
  grain-safety check from `_edge_is_grain_safe` lifted out)

**Phase 3b — Port to SemQL.** Three functions, one per
concern, in `logical.py` next to `build_join_graph` (line
418):

- `_validate_multi_cube_measure_refs(measures: list[ResolvedMeasure], measure_groups: dict[str, list[ResolvedMeasure]]) -> list[str]`
  — returns validation errors for measures that span
  multiple safe-merge groups. Empty list = OK.
- `_detect_single_group_fanout(group: MeasureGroup, dimensions: list[ResolvedDim], filters: list[str], join_graph: JoinGraph) -> tuple[bool, list[str]]`
  — returns `(has_fan_out, warnings)`.
- `_partition_into_safe_measure_groups(measure_groups: dict[str, list[ResolvedMeasure]], dim_sources: set[str], join_graph: JoinGraph) -> list[MeasureGroup]`
  — applies the safe-merge rules (alias siblings,
  `one_to_one` chains; **not** `many_to_one` chains).

Then the emission: a new function
`_emit_locality_query(env: _CompileEnv, plan: ResolvedPlan) -> CompiledQuery`
in `compile.py` alongside `_emit_simple_query` (line 1869)
and `_emit_compare_query` (line 1682). The shape:

- One `WITH` per `MeasureGroup`, alias `<cube>_agg` (with
  collision suffix counter).
- Outer `SELECT` with `COALESCE` over shared dim keys.
- Derived measures (when `InlineDerived` lands in C17) emit
  `COALESCE(agg, 0)` for non-divisors and
  `NULLIF(COALESCE(agg, 0), 0)` for divisors.
- `FULL JOIN` (or `JOIN` if `q.ungrouped` /
  `include_empty=False`) on shared dim keys.
- Per-measure `Measure.filter` (from C13) emits inside the
  per-group CTE, not at the outer level.

Phase 3b is ~600-800 LoC of new code plus ~20 new syrupy
snapshots. Three chasm-trap fixtures (two-fact-tables-
sharing-a-dim, three-fact-tables, asymmetric-grain) and two
fanout fixtures (one-fact-table-with-one-to-many-dim-join,
single-fact-with-filter-on-fanned-source).

### M4 — Two-tier loader + `engine.suggest()` (candidates 2, 5)

~3-4 days. Independent of M3 once the structural types are
in.

- **C2 — Two-tier loader.** Reject nested WITH in
  `DerivedTable.sql` (locked decision; the validator lands
  in M6). Add `tier: Literal["machine", "user"]` field on
  `Cube` (`model.py:477`). `semql-introspect` produces
  machine-tier cubes (default); user-authored cubes stay
  `tier="user"`. New `Overlay` value type in `model.py` for
  the merge construct:
  `Overlay(cube_name, column_overrides=..., exclude_columns=..., add_measures=..., add_segments=..., disable_joins=...)`.
  Merge rules in `catalog.py` next to `Catalog(...)` —
  strict, like ktx's `_compose` (load.py:134-237), but
  performed at *compile time* in
  `compile.py:_resolve_query_fields` so the catalog stays a
  clean two-tier split. The merge runs after the catalog is
  loaded; the resolved catalog and the resolved plan are
  two different stages.
- **C5 — `engine.suggest()`.** Sibling of `compile_query` in
  `compile.py`. Returns a
  `SuggestionReport(referenced_cubes, missing_cubes, disconnected_pairs, available_joins, remediation_hints)`.
  Uses `find_components` from the upgraded graph (C6) for
  the disconnected-pairs detection. ~150 LoC.

Tests: ~10 loader tests (machine-tier, user-tier, overlay,
validation errors); ~6 suggester tests (missing cube,
disconnected pair, ambiguous join, fanout warning, chasm
trap warning, empty catalog).

### M5 — Architectural lint (candidate 11)

~0.5 day. Independent. Could land first if desired; landing
here because it's the cheapest "insurance" milestone and
doesn't gate any other work.

New file: `scripts/check_boundaries.py` (Python, not Node,
since the rest of our tooling is Python). Runs as part of
`just check`. Rules to start with:

1. **No `BaseModel` subclass without `frozen=True` outside
   `packages/*/tests/`.** The allowlist is the catalog of
   frozen types we maintain: `BaseField`, `Cube`, `Measure`,
   `Dimension`, `TimeDimension`, `Segment`, `Join`,
   `NamedCTE`, `DerivedTable`, `PhysicalTable`, `Filter`,
   `BoolExpr`, `CompareWindow`, `InlineDerived`,
   `SemanticQuery`, `SavedQuery`, `SemanticQueryDefaults`,
   `AuthContext`, `MeasureGroup`, `ValidationReport`,
   `Overlay`, `JoinPath`. Anything else with `BaseModel`
   fails the check.
2. **No raw SQL f-strings outside the compiler core.** The
   allowlist: `compile.py`, `dialect.py`, `backend.py`,
   `parse.py`, `plan.py`, `logical.py`, `cnf.py`,
   `introspect.py`, `rollup.py`, `federate.py`. Detected by
   a `SELECT|FROM|JOIN|GROUP BY|ORDER BY` regex preceded by
   `f"` or `f'`. False-positive rate is non-zero; iterate
   on the rule.
3. **No `import` of `AuthContext` from `semql.auth` outside
   the documented allowlist:** `compile.py`, `introspect.py`,
   `auth.py`, `validate.py`, `prompt.py`, `hooks.py`, the
   semql-mcp package, and `tests/`. Forces the security
   boundary to stay visible.

Test file: `tests/test_check_boundaries.py` asserts the
check itself runs and that the allowlist hasn't drifted.
~30 LoC of test code; the check itself is ~80 LoC.

### M6 — Source CTE handling polish (candidate 16)

~0.5 day. Already a one-line decision (reject nested WITH);
this is the validation work.

- Extend `DerivedTable.sql` validation in
  `model.py:411-458` to call
  `sqlglot.parse_one(sql, dialect=...)` and check for any
  `exp.With` at the top level. If found, raise
  `ValidationError` with a migration hint: "lift inner CTEs
  to `with_ctes`; nested `WITH` in `DerivedTable.sql` is not
  supported."
- Add a syrupy fixture of a `DerivedTable` with a nested
  WITH to confirm the error message is clear.
- Audit existing `with_ctes` users (search across tests +
  demos) to make sure no test relies on the nested-WITH
  form.

## Risks

**R1 — Snapshot churn.** The cheap-wins batch (M1) doesn't
change emitted SQL for existing queries, so existing
snapshots should be stable. C7 (reserved-identifier
quoting) and C13 (filter injection) are the only candidates
that touch emission in M1; both should produce identical
output for queries that don't use reserved words and don't
have per-measure filters. The snapshots in M1 are
*additive* (new fixtures for the new cases), not
replacements. M2's C6 (Dijkstra) could change join order for
ambiguous joins; expect 1-2 snapshots to need updating. M3
will add new emission paths but not change the simple path.

**R2 — Anchor semantics.** ktx's `_pick_anchor`
(`planner.py:738-759`) is a deliberate rule. SemQL uses
`touched[0]` implicitly. When we add anchor selection
(probably as part of M3, since chasm-trap detection uses
it), the question is whether to expose it as a query-time
option (`SemanticQuery.anchor_cube: str | None = None`) or
keep it implicit. The locked decision is to keep things
opinionated; default to implicit, allow override later. No
new spec field in M3.

**R3 — `Measure.filter` doesn't exist in SemQL today.** C13
(the filter injection helper) is a pure function and ships
in M1, but there's no `Measure.filter` field for it to read
from. The wiring happens in M3, when per-measure filters
become useful (per-group CTEs). Adding
`filter: str | None = None` to `Measure` in `model.py:166`
is a ~3-line change; it should land as a small commit at
the start of M3a, not in M1.

**R4 — InlineDerived cross-cube.** C17 lifts the Phase A
restriction. The new resolver walks the operand graph; if a
cross-cube operand is referenced, the compiler needs to know
the operand is reachable via the join graph. This is *not*
a chasm-trap problem (it's a same-measure, different-cube
reference), but it needs the upgraded graph (C6) to be safe.
C17 should land *after* C6 in M2, not in M1. (Originally
planned in M1; moved to M2 after the dependency was
identified.)

**R5 — The depth-pass candidates #11-17 in the spec doc are
not all in this plan.** Mapping check:

- 12, 13 → M1
- 6, 14, 17 → M2 (15 dropped per the no-HAVING decision)
- 1 → M3
- 2, 5 → M4
- 11 → M5
- 16 → M6

All 17 are covered. The spec doc is now out of order vs.
the milestones; the ordering here is the source of truth.

**R6 — AGENTS.md / PHILOSOPHY.md updates.** Each milestone
that adds a new value type or new emission path should
update `AGENTS.md` and possibly `PHILOSOPHY.md`. M1's
"DIALECT CONVENTION blocks" (C8) is the obvious place; M3's
chasm-trap detection should be added to `PHILOSOPHY.md` (it's
a load-bearing compiler invariant). M5's lint rules should
be in `AGENTS.md` so contributors know what to expect.

**R7 — Test infrastructure assumptions.** This plan assumes
`test_plan_snapshots.py` and `test_snapshots.py` patterns
are stable. Worth a quick check at the start of each
milestone: if a milestone needs new fixture categories
(e.g. M3's chasm-trap fixtures), confirm `tests/conftest.py`
has the right factory.

## Sequencing summary

| Milestone | Candidates | Effort | Dependencies |
|-----------|-----------|--------|--------------|
| M1 Cheap wins | C3, C4, C7, C8, C9, C12*, C13 | ~1 day | none |
| M2 Structural | C6, C14, C17 (C15 dropped) | ~3-4 days | M1 |
| M3 Chasm-trap | C1, plus `Measure.filter` field | ~1-2 weeks | M2 |
| M4 Loader + suggester | C2, C5 | ~3-4 days | M2 |
| M5 Lint | C11 | ~0.5 day | none |
| M6 Source CTEs | C16 | ~0.5 day | none |

*C12 conditional on whether SemQL currently rejects
subqueries in WHERE. Verify first.

M1 + M5 + M6 are independent of M3. They could ship in any
order. M2 blocks M3. M3 is the centerpiece. M4 is
independent of M3 (parallelisable).

End-to-end calendar:

- Serial: ~3-4 weeks
- With M5 and M6 in parallel: ~3 weeks
- With M1+M2+M3 on a TDD spike and M4+M5+M6 on a parallel
  branch: ~2.5 weeks

## Open questions to verify before M1 starts

1. **C12 assumption.** Does `validate.py` currently reject
   subqueries in filters? Quick grep of
   `validate.py:_validate_*` should tell us. If SemQL
   already rejects, drop C12 from M1.
2. **Snapshot baseline.** Confirm the snapshot count in
   `tests/__snapshots__/` so we have a baseline for churn
   risk. The C7 / C13 / C6 changes are the most likely
   sources of churn.
3. **Per-milestone goal packaging.** Decide whether to open
   the work as a single goal (one mega-PR) or as a
   `plannotator-setup-goal` per milestone. My recommendation:
   one goal per milestone, since each is independently
   shippable and the test surfaces don't overlap.

## What happens after this plan

Ready to convert any of these milestones into a goal package
when given the word. The first concrete action is verifying
R1 (the C12 assumption); one grep in `validate.py` will tell
us whether M1 shrinks to 6 candidates or stays at 7.
