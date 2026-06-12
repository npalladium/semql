# Spec: Borrowable Patterns from ktx (Kaelio/ktx)

## Overview

Evaluation of patterns borrowable from
[Kaelio/ktx](https://github.com/Kaelio/ktx) for SemQL, screened
against `PHILOSOPHY.md` and the existing `TODOS.org` backlog. The
exploration was a depth-1 clone of ktx into `.scratch/ktx/` on
2026-06-10; the relevant subpackage is `python/ktx-sl/` (5,303 LoC,
sqlglot + Pydantic).

## Scope of this doc

Decision record, not a feature backlog. For each candidate:

1. What it would look like in SemQL
2. Compatibility with `PHILOSOPHY.md`
3. Effort estimate
4. Recommendation (implement / defer / reject / document)

ktx is the closest open-source analogue to SemQL we have found —
same problem domain (yaml-defined semantic layer over a SQL warehouse,
sqlglot AST, Pydantic catalog/spec, prompt pipeline at the agent
boundary), and a busy codebase (~290 PRs at v0.11.0). The compiler
is the load-bearing piece; the TS CLI around it is agent-runtime
plumbing we don't need to study in depth.

The single biggest divergence: **ktx's Python compiler has no
authorisation, no row-level security, no viewer/role model**. Grep
for `AuthContext`, `viewer`, `scope_fn`, `ScopeFn`, `RLS`,
`row-level security`, `tenant` across `python/ktx-sl/` returns
nothing. The compiler emits SQL and trusts the warehouse connection
to enforce isolation. This validates SemQL's design choice; do not
drop `AuthContext` / `ScopeFn` / `security_sql` plumbing.

## Candidate 1: Chasm-trap detection + per-source CTE local aggregation

**ktx source:** `python/ktx-sl/semantic_layer/planner.py:932-1190`
(`_detect_fan_out`, `_merge_safe_measure_groups`) and
`generator.py:_generate_with_locality`.

**What it is.** When measures span multiple fact sources that all
join to a shared dimension, naïve `GROUP BY` across the joined fact
set causes fanout (a measure gets multiplied by the cardinality of
the unrelated side) or a chasm trap (two measures' filters can't
both be expressed as the same WHERE/HAVING shape). ktx:

1. Groups measures by source into `MeasureGroup`s
   (`planner.py:_detect_fan_out`).
2. Classifies each join path as `one_to_one` / `many_to_one` /
   `one_to_many` and only allows safe merges (alias siblings merge
   freely; `one_to_one` chains merge; `many_to_one` chains do **not**
   merge because the "one" side would be duplicated by the "many"
   side rows).
3. Emits one CTE per safe-mergeable group, joined with `FULL JOIN`
   (or `JOIN` when `include_empty=False`) on the shared dimension
   keys. Non-shared dimensions are wrapped in `COALESCE` at the
   outer SELECT. Cross-CTE derived measures are wrapped in
   `COALESCE(..., 0)` for non-divisors and
   `NULLIF(COALESCE(..., 0), 0)` for divisors.
4. Asymmetric dimension grain (group A has `{region, country}` but
   group B can only reach `{region}`) is a hard error, not a silent
   fanout.

**SemQL gap.** `compile.py` does a basic BFS for the join graph and
produces a single SELECT. There is no fanout / chasm-trap concept.
The TPC-H snapshots in `tests/__snapshots__/` exercise the happy
path only.

**Compatibility with PHILOSOPHY.md.** Aligned. The compiler stays
pure; the chasm-trap detector is a planning concern that runs before
sqlglot composition. The new emitted SQL shape is still
postgres-shaped, transpiled at the end (matches our existing
`compile.py` discipline).

**Effort estimate.** 600-800 LoC plus a discovery spike. The
function is dense (~250 LoC of branching) and would need to be
refactored per concern before merging. New test surfaces: at least
3 chasm-trap fixtures and 2 fanout fixtures, each with a syrupy
snapshot.

**Recommendation: implement.** This is the load-bearing reason to
read ktx at all. The alternative — leaving the LLM to write the SQL
for this case — is exactly the failure mode `PHILOSOPHY.md` exists
to prevent. Open as a TDD spike in `packages/semql/`, port
`_detect_fan_out` against existing TPC-H snapshots to see how the
join graph needs to change.

## Candidate 2: Two-tier catalog loader — `_schema/*.yaml` + overlays

**ktx source:** `python/ktx-sl/semantic_layer/loader.py:35-114`,
`manifest.py:190-227` (`project_manifest_entry`, `validate_overlay`).

**What it is.** Splits "what the warehouse scan produced" from
"what the analyst asserts":

- `_schema/<connection>/*.yaml` — auto-generated shards, machine-
  friendly, hold physical-table metadata.
- `<connection>/*.yaml` outside `_schema/` — user-authored. Files
  with `sql:` or `table:` are standalone sources. Files without
  either are **overlays** that compose on top of the matching
  manifest entry (`_compose` at `loader.py:134-237`).
- Overlay rules reject unsafe merges via `validate_overlay`
  (`manifest.py:146-187`).

**SemQL gap.** `Catalog` / `Cube` is single-tier. The
`semql-validate-db` package already exists as a separate drift
check; it has no shared types with the catalog.

**Compatibility with PHILOSOPHY.md.** Aligned. Keeps the catalog
clean and machine-checkable. The two tiers would naturally pair
with `semql-validate-db` owning the machine-tier.

**Effort estimate.** 1-2 days for the value type split, 1-2 days
for overlay composition, plus API breakage review. Touches the
loader and the introspection package.

**Recommendation: implement.** Pair with `semql-validate-db` to
keep the machine-tier out of the user-facing API surface.

## Candidate 3: `Provenance` enum on resolved fields

**ktx source:** `models.py:183-187`.

**What it is:**

```python
class Provenance(str, Enum):
    VERIFIED = "verified"     # pre-defined measure
    COMPOSED = "composed"     # ad-hoc expression
    DIMENSION = "dimension"   # raw dimension column
```

Carried on `ResolvedMeasure` and `ResolvedColumn`. Tells downstream
consumers (MCP tools, presenters, drilldown) how much to trust a
value.

**SemQL gap.** `ResolvedField` (or whatever the spec output type is
called) has no provenance flag. The Presenter and Drilldown roles
in the prompt pipeline have no typed signal to say "this number
came from an approved measure definition" vs. "the LLM made this up
from a raw column."

**Compatibility with PHILOSOPHY.md.** Aligned. Frozen enum, no
runtime cost.

**Effort estimate.** < 1 day. Adds one field to the resolved
value type, populates it in the planner, threads it through. Worth
a `TestProvenance` table-driven test.

**Recommendation: implement.** Cheap, typed, and materially
improves the prompt pipeline's ability to ask the user "are you
sure?" when an ad-hoc measure gets drilldowned.

## Candidate 4: `ValidationReport` value type (errors + warnings + per_source_warnings)

**ktx source:** `models.py:258-264`.

**What it is:**

```python
class ValidationReport(BaseModel):
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    per_source_warnings: dict[str, list[str]] = Field(default_factory=list)
```

**SemQL gap.** `CompileError` is fatal-only. There is no channel
for "this works but I want to warn you about X" — useful for
ambiguous-join picks, fanout-but-might-be-fine cases, deprecated
identifiers, and missing-but-optional context (e.g. no
`description` on a `Dimension`).

**Compatibility with PHILOSOPHY.md.** Aligned. Frozen, structured,
machine-checkable. The `errors`/`warnings` split matches the spirit
of "the compiler refuses silently-bad queries loudly" — adding a
non-fatal channel doesn't weaken the fatal one.

**Effort estimate.** < 1 day. New value type + plumbing through
`compile()`.

**Recommendation: implement.** Pair with Candidate 1 (chasm-trap)
where many cases are warnings rather than errors, and with
Candidate 6 (ambiguity detection) below.

## Candidate 5: `engine.suggest()` — error-as-recovery-plan

**ktx source:** `engine.py:337-475`.

**What it is.** When planning fails, the engine surfaces: which
sources are referenced, which are missing, which pairs are
disconnected, the join graph's connected components, and concrete
remediation hints ("add join X → Y on `…`"). The output is a
structured value the CLI / MCP tool can render.

**SemQL gap.** `CompileError` carries a message string and (in
some cases) a location. There is no structured recovery plan. The
Router role in the prompt pipeline has to parse the error message
to know what to ask the user next.

**Compatibility with PHILOSOPHY.md.** Aligned. Doesn't change
compiler purity; it's a sibling of `compile()` that walks the same
catalog + query and produces a different shape of output.

**Effort estimate.** 1-2 days. Most of the work is in catalog
introspection (`iter_cubes`, `iter_joins` already exist; need a
`connected_components` helper on the join graph).

**Recommendation: implement.** Cheap relative to its value for
LLM-driven flows (Router, Presenter, Drilldown) that need to ask
the user a useful follow-up instead of "compile failed: no
matching cube."

## Candidate 6: Steiner-tree + Dijkstra with cost weighting + ambiguity detection

**ktx source:** `python/ktx-sl/semantic_layer/graph.py:24-285`.

**What it is.** ktx weights `one_to_many` edges at 10× cost over
`many_to_one` / `one_to_one`, picks the cheapest path, and flags
equal-cost alternatives as `is_ambiguous=True` (`graph.py:140-141`)
so the generator can warn. `resolve_join_tree` is a Steiner-tree
approximation rooted at the user's chosen anchor (graph.py:160-205).

**SemQL gap.** `compile.py` does a basic BFS. There is no cost
weighting, no ambiguity detection, and no anchor concept — the
join order is whatever BFS produces.

**Compatibility with PHILOSOPHY.md.** Aligned. Pure planning step.

**Effort estimate.** < 1 day. ~60 LoC of graph algorithm plus
plumbing the `is_ambiguous` flag through the resolved plan.

**Recommendation: implement.** Pair with Candidate 4 so the
ambiguity is surfaced as a `ValidationReport` warning, not an
exception.

## Candidate 7: Reserved-identifier quoting

**ktx source:** `python/ktx-sl/semantic_layer/parser.py:132-196`.

**What it is.** Mask string literals → find `word.word` patterns →
selectively double-quote parts that are SQL reserved words. Closes
a class of bugs on `group.key`, `order.status`, `select.count`.

**SemQL gap.** `compile.py` relies on sqlglot's default identifier
handling, which fails on reserved words.

**Compatibility with PHILOSOPHY.md.** Aligned. Keeps sqlglot as
the SQL structure source-of-truth; just pre-quotes identifiers
before parsing.

**Effort estimate.** < 0.5 day. ~40 LoC plus a fixture for
`group.key` and `order.status` cases.

**Recommendation: implement.** Smallest lift, closes a real bug
class.

## Candidate 8: Module-top "DIALECT CONVENTION" comment blocks

**ktx source:** `planner.py:27-34`, `generator.py:20-29`,
`graph.py:9-12`, `parser.py:10-14`.

**What it is.** Each module that touches SQL starts with a
3-7-line comment block stating the dialect convention:
"user-authored `expr` parses with `read=self.dialect`; the skeleton
writes postgres; `_transpile()` once at the end." Other conventions
(parse-cache locality, lru_cache key shape) are stated the same
way.

**SemQL gap.** The dialect policy lives implicitly in the code
shape. There's no single place a new contributor can read to know
"this is how dialects work here."

**Compatibility with PHILOSOPHY.md.** Aligned. Comments, not code.

**Effort estimate.** < 1 hour.

**Recommendation: implement.** No-brainer. Add the same block to
`packages/semql/semql/compile.py`, `dialect.py`, and `parser`
files.

## Candidate 9: `parse_cache` (lru_cache on `(sql, dialect)`)

**ktx source:** `parser.py:199-206`.

**What it is.** `functools.lru_cache(maxsize=256)` keyed on
`(sql, dialect)`. Same measure `expr` strings get parsed many times
per compilation.

**SemQL gap.** `compile.py` re-parses the same `expr` strings
every time a query references them.

**Compatibility with PHILOSOPHY.md.** Aligned. Pure-function cache
on a pure parser — no I/O, no globals beyond the cache.

**Effort estimate.** < 1 hour. One decorator.

**Recommendation: implement.** Pair with Candidate 7.

## Candidate 10: Symmetric `from_sources()` factory

**ktx source:** `engine.py:28-36`.

**What it is.** The same engine can be built from a directory
(production) or from an in-memory `dict` (tests, daemon wire
transport). Symmetric with SemQL's `Catalog.from_yaml(...)` vs.
`Catalog(...)` — except ktx uses one method that dispatches on
input type, which is cleaner.

**SemQL gap.** Already mostly aligned: `from_catalog(...)` is a
classmethod. Worth a quick audit.

**Effort estimate.** < 0.5 day. Audit + maybe one new constructor.

**Recommendation: audit, don't add yet.** The current shape is
fine. Add an audit comment to `model.py` and move on.

## Candidate 11: Architectural lint as a CI check

**ktx source:** `scripts/check-boundaries.mjs` (and a test
asserting the check stays active).

**What it is.** Greps for forbidden cross-layer imports (e.g.
direct `createAnthropic` outside the LLM layer) and fails CI.
Architectural rules become machine-enforced, not just
documented.

**SemQL gap.** `PHILOSOPHY.md` invariants ("no raw SQL f-strings
outside `compile.py`", "no `frozen=False` Pydantic models outside
test fixtures") are convention-only. Nothing in `just check`
enforces them.

**Compatibility with PHILOSOPHY.md.** Strongly aligned. This is
PHILOSOPHY.md *as a CI gate*.

**Effort estimate.** 0.5 day. One Python file with regexes, one
test asserting the check runs, one CI job (or a `just check`
target).

**Recommendation: implement.** Start with two or three rules,
grow from there. Candidates for the first wave: (1) no
`SELECT`/`FROM`/`JOIN` literal in non-`compile.py` / `dialect.py`
files, (2) no `BaseModel` without `frozen=True` outside
`packages/semql/tests/`.

## Anti-patterns to avoid lifting

1. **No `frozen=True` on Pydantic models.** ktx builds on
   `BaseModel` only and relies on convention plus
   `model_copy(update=…)`. Mutating a non-frozen Pydantic model
   after construction silently works in Python. Directly
   contradicts SemQL's PHILOSOPHY.
2. **No parameter binding.** `QueryResult.sql: str` only — filter
   values are inlined by sqlglot during transpile. No prepared
   statements, no audit trail of injected values. The opposite of
   SemQL's `CompiledQuery.params` discipline. Do not replicate.
3. **The `_detect_fan_out` function is ~250 LoC of dense branching**
   with three interleaved concerns (alias siblings, `many_to_one`
   chain safety, asymmetric dimension grain). If we lift it
   (Candidate 1), refactor per concern first before merging.
4. **No `mypy`/`pyright` in CI.** Don't take inspiration from
   this.

## Things SemQL does that ktx doesn't (worth remembering)

- `AuthContext` + `ScopeFn` + `security_sql` — fundamental.
- `CompiledQuery.params` — prepared-statement-friendly.
- `frozen=True` Pydantic value types — load-bearing.
- Two type checkers in strict mode — load-bearing.
- `PHILOSOPHY.md` invariants — ktx has the equivalent in
  `AGENTS.md` (27 KB, MUST/SHOULD/MAY rules) but it's all
  convention. Candidate 11 is the chance to make ours enforced.
- 4-role typed-Pydantic prompt pipeline (Router / Generator /
  Presenter / Drilldown) — ktx has skills + memory agents but
  no equivalent typed-output chain. The closest thing is
  `packages/cli/src/skills/sl_capture/SKILL.md`, which is a
  single skill, not a pipeline.

## Recommended next steps

1. **Chasm-trap + aggregate-locality** (Candidate 1) — open as a
   TDD spike in `packages/semql/`. Highest value, highest risk,
   deserves its own goal.
2. **Two-tier loader** (Candidate 2) — touch the loader and the
   introspection package. Pair with `semql-validate-db`.
3. **The cheap wins** (Candidates 3, 4, 7, 8, 9) — batch as a
   single "borrow from ktx" PR. Each is < 50 LoC.
4. **Architectural lint** (Candidate 11) — start with 2-3 rules,
   grow from there. ~0.5 day.
5. **Defer** Candidates 5 and 6 until Candidates 1 and 4 are in.
6. **Audit, don't add** Candidate 10.

## Depth pass: ktx compiler internals

Re-read all five layers end-to-end on 2026-06-10. The summary
above is correct; this section records the specific things that
became clearer on the second read and updates the recommendation
table with concrete deltas. Every claim is keyed to a file and
line range in the ktx clone.

### ktx pipeline shape (the mental model)

`engine.SemanticEngine.query` (`python/ktx-sl/semantic_layer/engine.py:47-60`)
is the only public entry point. It calls `planner.plan(query)` then
`generator.generate(plan, sources)`. The planner returns a
`ResolvedPlan` (a frozen Pydantic value type) and the generator
turns that into a SQL string. The two layers are connected only
through the resolved plan, not through shared state — same pattern
as SemQL's `_CompileEnv` + `compile_query` split. **Confirmed this
is a load-bearing boundary: the planner can be tested without
sqlglot, the generator can be tested against canned plans.**

The planner is 1,445 lines and is the bulk of the work. It has 13
named steps, each numbered in source comments. The generator is
1,425 lines and has two code paths (simple and aggregate-locality)
selected by `plan.has_fan_out`. The parser (303 lines) is
intentionally narrow: it wraps sqlglot AST walks and produces a
`ParsedExpression` struct consumed by the planner. The graph (285
lines) is pure graph algorithms over `JoinEdge` / `JoinPath` /
`JoinTree` dataclasses.

The loader is the smallest (256 lines) and the most distinctive —
it's the only place in ktx that does I/O. Everything else is pure
data-in / data-out.

### Per-layer depth findings

#### `loader.py` — the only I/O surface

The two-tier split is the entire design (`loader.py:39-114`):

1. `_schema/<connection>/*.yaml` — auto-generated, **never
   edited by humans**. Machine-friendly. Defines tables, columns,
   joins, descriptions, grain, measures, segments.
2. `<connection>/*.yaml` outside `_schema/` — user-authored.
   Two sub-cases:
   - Has `sql:` or `table:` → standalone (rare, the "I know
     better than the scan" case).
   - Has neither → overlay that composes on top of the matching
     manifest entry via `_compose` (`loader.py:134-237`).

The `_compose` method is the heart of the design. It does a
`deepcopy(base)` then merges overlays in a strict order:
`descriptions` → `exclude_columns` (with conflict detection vs.
`column_overrides`) → `column_overrides` (metadata patches) →
`columns:` (new computed columns only — patching a manifest
column with `columns:` is a hard error directing you to
`column_overrides`) → `measures` / `segments` (full replace, not
merge) → `grain` (override) → `joins` (union + dedupe keyed on
`to::on` whitespace-normalized) → final invariant check
(table-or-sql, not both).

The merge rules are tight: descriptions deep-merge, columns
disjoint, joins set-semantic, measures/segments wholesale
replace. The errors are concrete
(`"column 'X' in columns patches a manifest column on 'Y' - move
it to 'column_overrides:'"`).

`validate_overlay` (`manifest.py:146-187`) is the gatekeeper. It
runs before `_compose`, so bad overlays never reach the merge
logic.

**Cross-reference to SemQL.** SemQL has no two-tier concept. The
catalog is single-tier and the introspection package produces a
plain `Catalog` that gets serialized back to YAML. The ktx pattern
would let `semql-introspect` own the machine tier and the user
keep their hand-authored catalog clean. The lift is well-bounded
because the merge rules are explicit and small.

#### `graph.py` — real Dijkstra with ambiguity

The single most interesting function is `find_path`
(`graph.py:106-158`). It is **not** a BFS pretending to be
Dijkstra: it uses `heapq` with `(cost, counter, node, path)`
tuples, weights `one_to_many` edges at 10× the cost of
`many_to_one` / `one_to_one`, and has a **deliberate
ambiguity-detection loop** that pops extra equal-cost paths after
the first arrival and flips `first_path.is_ambiguous = True`
(`graph.py:138-141`).

The "ambiguity" output is consumed by `resolve_join_tree`
(`graph.py:160-205`) which logs a warning when it had to pick
arbitrarily between equal-cost paths. **This is the pattern
SemQL should adopt** — SemQL's `find_join_path` (`logical.py:447-485`)
is plain BFS with no cost weighting and no ambiguity signal.

`build()` (`graph.py:68-104`) builds a bidirectional adjacency
list keyed on source name. Aliases (`join.alias`) get their own
adjacency entries and a `alias_map: dict[str, str]` for
dereferencing at query time. Alias siblings are *separate
adjacency nodes*; the planner merges them when it's safe to do
so, not the graph.

`_parse_on` (`graph.py:244-285`) parses a join `on:` clause
using sqlglot, extracts column pairs from `exp.EQ` nodes, and
returns a `(from_cols, to_cols)` tuple with comma-separated
strings for composite keys. It rejects nested equality (`a = b =
c`) — a tiny but real correctness check.

`find_components` (`graph.py:207-242`) does BFS over the
adjacency, treating aliases as same-component as their base.
Used by `engine.suggest()` for "sources X and Y are
disconnected" diagnostics.

**Recommendation update for Candidate 6** (steiner-tree + Dijkstra
+ ambiguity): the lift is the `find_path` function, ~60 LoC of
real graph algorithm plus the `is_ambiguous` flag threaded
through the join plan. The `find_components` function is a
separate, even smaller lift (Candidate 5) — maybe 30 LoC on top
of what's already in `_resolve`/`introspect`. Pair them, and
the warning fires from a structured `ValidationReport` warning
(Candidate 4).

#### `parser.py` — narrow, opinionated, with a real cache

The whole file is one dataclass (`ParsedExpression`,
`parser.py:139-147`), one regex-based helper
(`quote_reserved_identifiers`, `parser.py:157-196`), one
`lru_cache`-wrapped parse (`_cached_parse_select`,
`parser.py:199-206`), and one class (`ExpressionParser`,
`parser.py:209-303`).

The reserved-word set is hand-maintained (`_SQL_RESERVED`,
`parser.py:42-130`, ~80 words) and includes cross-dialect
additions like `glob`, `qualify`, `rlike`. The regex approach
is correct but: it masks string literals first, then matches
`\b(\w+)\.(\w+)\b`, then double-quotes parts that are reserved.
This is the only regex-on-SQL in the compiler and the comment at
`parser.py:10-14` explicitly contracts its use.

`ExpressionParser.parse` returns a `ParsedExpression` with:
- `source_refs`: set of source names referenced (from
  `exp.Column.table`)
- `column_refs`: set of `source.column` strings
- `is_aggregate`: `True` if any `exp.AggFunc` is found **outside
  of a subquery** (`_inside_subquery`, `parser.py:254-260`)
- `aggregate_function`: name of the first aggregate
- `has_window_function`: presence of `exp.Window`
- `depends_on_measures`: bare identifiers that match a
  `known_measure_names` set

The `_inside_subquery` check is the load-bearing insight: it
makes `col = (SELECT MAX(col) FROM t)` classify as a plain
column predicate, not a HAVING candidate. **SemQL has no
equivalent of this.** Our filter classifier
(`compile.py:_predicate_term`) doesn't currently know how to
walk past subqueries. If we ever support subquery-bearing
filters, this is the pattern to copy.

The lru_cache is keyed on `(sql, dialect)`, maxsize 256, and
sits at module scope. It's a free perf win — same measure
`expr` strings get parsed many times per compilation because
the planner re-parses them in `_extract_predefined_refs`,
`_match_predefined_ref`, `_resolve_measure_str`, etc.

Custom aggregates (`count_distinct`, `percentile`, `median`)
are detected by name on `exp.Anonymous` nodes
(`parser.py:272-277`) and translated in the generator
(`generator.py:_translate_custom_funcs`, `generator.py:721-763`).
This is a small, clean pattern for "the catalog exposes a
function the user wrote that sqlglot parses as Anonymous."

**Recommendation update for Candidates 7 and 9** (reserved-word
quoting + parse cache): both are confirmed single-file, < 1
day total. The `_inside_subquery` aggregate detection is a
**third** candidate worth adding to the spec — a real
correctness pattern, ~10 LoC.

#### `planner.py` — 13 steps, one hard function

This is the bulk of the work. The 13 numbered steps in `plan()`
(`planner.py:51-169`) execute in order, each a focused
helper. The full step list, with the load-bearing details:

0. `_validate_visibility` (`planner.py:1373-1407`) — rejects
   queries that touch hidden columns. SemQL has a similar
   field-visibility concept but expressed differently (per
   `field.metadata` rather than `ColumnVisibility.HIDDEN`).

1. `_resolve_dimensions` (`planner.py:171-186`) — qualifies
   bare column names that uniquely identify a single source
   column (`_qualify_bare_column`, `planner.py:188-205`).
   Throws on ambiguity, leaves unknowns for downstream errors.

2. `_resolve_measures` (`planner.py:207-236`) — the biggest
   sub-pipeline. It has its own sub-steps:
   - `_collect_colliding_predefined_names` (`:238-259`): pre-
     pass to detect measures that would collide after
     qualification, so we can rename them in one place.
   - `_resolve_measure_str` (`:525-612`): for `"orders.revenue"`
     strings, try the predefined path; for `"sum(orders.amount)"`
     strings, build a `COMPOSED` measure; for bare identifiers,
     try unqualified resolution against the catalog.
   - `_resolve_measure_dict` (`:614-691`): for `{"expr": ...,
     "name": ...}` dicts, with full predefined-ref rewriting
     via AST transform.
   - `_expand_predefined_chains` (`:415-523`): recursive
     expansion of derived measures that reference other derived
     measures, with memoization on `expanded: set[str]`.
   - `_auto_add_predefined_deps` (`:359-395`): if a derived
     measure references a predefined measure not in the query,
     add it (positioned before the derived measure in the
     output list, so deps come first).
   - `_qualify_duplicate_names` (`:397-413`): when two sources
     expose the same measure name, suffix the duplicates with
     `f"{source_name}_{measure_name}"`.

3. `_topological_sort_measures` (`planner.py:713-736`) — DFS
   with `in_stack: set[str]` for cycle detection. Raises on
   cycle. SemQL's `InlineDerived` doesn't currently support
   `derived.derived` chains or surface depends_on metadata, so
   this is **strictly new territory**. Phase A explicitly
   requires all operands on the same cube; topo-sort would
   unlock cross-cube chains.

3a. `_apply_query_segments` (`:793-860`) — ANDs each
   query-time segment into the filter of every measure whose
   base source matches the segment's source. Throws if a
   query-time segment has no matching measure (vs. silently
   dropping it).

3b. `_validate_column_refs` (`:1322-1371`) — every column
   ref must exist on its source. Separates dimension refs
   (only columns allowed) from measure/filter refs (columns +
   measure names allowed).

4. **Collect source refs** (in-line `:71-83`) — walks every
   measure / dimension / filter expression, runs
   `parser.extract_source_refs`. Throws if `not source_refs`.

5. `_pick_anchor` (`:738-759`) — anchor selection logic.
   When `include_empty=True`, prefers a dimension's source.
   Otherwise, prefers the first non-derived measure's source.
   Falls back to the first dimension's source, then the
   alphabetically smallest source ref. **SemQL has no anchor
   concept** — `touched[0]` is implicit.

6. `graph.resolve_join_tree` — Steiner approximation rooted
   at the anchor. (Already covered in graph.py notes.)

7. **Build `ResolvedJoin`s** (in-line `:98-108`) — struct
   projection of the tree edges.

8. `_detect_fan_out` (`:932-1111`) — **the load-bearing
   function**. Returns
   `(has_fan_out, measure_groups, fan_out_desc, locality_descs)`.
   Logic is dense; the candidate-1 effort estimate stands at
   600-800 LoC. The function has three concerns interleaved:
   - **Multi-source validation** (`:952-979`): a non-derived
     measure that references sources from multiple groups
     raises (can't fit in one CTE).
   - **Single-group fanout detection** (`:992-1039`): walk
     paths from the measure source to each dim source; flag
     if any path has `one_to_many` edges. **Filter sources**
     are also checked — a filter on a one_to_many source
     from the measure source is a hard error.
   - **Multi-group chasm detection** (`:1041-1111`): try
     `_merge_safe_measure_groups` first; if multiple safe
     groups remain, it's a true chasm trap. Validate that
     every filter source is reachable from at least one
     measure source without crossing `one_to_many` (silent
     drop in CTE mode is a hard error).

   The 250-LoC density is the *one* reason this is not
   already a 1-day lift. Refactor targets:
   `_validate_multi_source_measure_refs`,
   `_detect_single_group_fanout`,
   `_detect_chasm_trap_groups` as three separate
   functions.

9. `_classify_filters` (`:1214-1247`) + `_classify_filter_clause`
   (`:1192-1212`) — splits filters into WHERE vs. HAVING
   based on:
   - Is the expression aggregate? → HAVING
   - Does it depend on a measure? → HAVING
   - Does it reference a predefined measure by `source.measure`
     where `measure` is *not* also a column name? → HAVING
   - Otherwise → WHERE

   The classifier uses `_split_top_level_and` (`:1295-1320`)
   to handle `a AND b` as two independent clauses, and
   `_validate_or_filter_consistency` (`:1249-1293`) to reject
   `aggregate OR non_aggregate` (can't split).

   **SemQL has no HAVING concept** — our
   `_compile_where_tree` is the only filter renderer. If we
   ever add a per-measure filter (we don't have it; closest is
   `Measure.filter` on the catalog side), we'd need this
   machinery.

10. **Compute anchor grain** (in-line `:119-126`) — currently
    just `anchor_grain = [d.field for d in dimensions]`. The
    shape is right; the actual "anchor grain" reasoning (does
    every cube's group-by stay at the anchor's grain?) is
    absent. This is where SemQL's `wrap_for_tenancy` is
    stronger — we know each cube's grain from the catalog.

11. `_build_columns` (`:1409-1445`) — handles leaf-name
    collisions in dimensions (`region.region` →
    `orders_region`, `customers_region`). Carries
    `Provenance.DIMENSION` on dimension columns,
    `m.provenance` on measure columns.

12. **Build join path descriptions** (in-line `:131-140`) —
    human-readable string per join, used for plan-rendering
    and the prompt pipeline. Cheap, ~10 LoC, worth lifting
    for SemQL's `plan.explain()`.

13. **Resolve order_by** (in-line `:142-149`) — accepts
    either dict (`OrderByClause(**ob)`), string
    (`OrderByClause(field=ob)`), or pass-through. Then
    `_resolve_order_field` (`generator.py:928-951`) does the
    resolution against measures and dimensions.

`return ResolvedPlan(...)` — single structured return value.

**Cross-reference to SemQL.** The 13-step shape is the same
one SemQL's `_CompileEnv.__init__` executes (visibility,
resolve, validate, anchor-equivalent — though implicit —
fanout-equivalent — though absent — classify, group, emit).
The big deltas:

- **Steps 1, 2, 3, 3a, 3b, 8, 9** are mostly absent in
  SemQL. We have `InlineDerived` (Phase A, single-cube)
  but no chain expansion, no cycle detection, no
  measure-name disambiguation across sources, no HAVING.
- **Step 5** has no counterpart. We use `touched[0]` as the
  FROM root.
- **Step 8** is the candidate-1 lift.
- **Step 9** is a real gap if we ever add per-measure
  filters.

#### `generator.py` — two paths, one transpile

The entry point is `generate(plan, sources)`
(`generator.py:74-93`). It builds source CTEs first, then
picks a path based on `plan.has_fan_out`, then transpiles the
outer scaffold exactly once.

The `has_fan_out` branching is a clean if/else, not a strategy
pattern:

```python
if plan.has_fan_out and plan.measure_groups:
    outer_sql = self._generate_with_locality(plan, sources)
else:
    outer_sql = self._generate_simple(plan, sources)
outer_transpiled = self._transpile(outer_sql)
if not native_source_ctes:
    return outer_transpiled
# ... concatenate with WITH clause
```

**Source CTE handling** (`_build_source_ctes`,
`generator.py:515-552`) is novel: it parses user-authored
`sql:` source bodies, extracts any nested `WITH` clauses,
prefixes their aliases with `f"{source_name}__{inner}"`, and
hoists them to the outer `WITH`. This is a real correctness
move — without it, inner CTEs with generic names like
`active_users` would collide across sources.

**Path A — `_generate_simple`** (`:97-160`) is what SemQL's
`build_inner` does: SELECT → FROM → JOINs → WHERE → GROUP
BY → HAVING → ORDER BY → LIMIT. Notable details:
- `SELECT DISTINCT` when no measures (dimension-only
  listing).
- `_qualify_filter` (`:1324-1328`) just runs
  `_expand_computed_columns`. Filter qualification is
  light because the AST is already column-shaped.
- `ORDER BY` defaults to dim positions when no explicit
  `order_by` clause (preserves stable order).

**Path B — `_generate_with_locality`** (`:164-339`) is the
chasm-trap compiler. The high-level shape:
1. For each measure group, compute which dimension keys it
   can safely reach (`:173-192`).
2. Compute **shared dimension aliases** — dims reachable
   from *all* groups. These become the `FULL JOIN` keys.
3. **Validate grain consistency** (`:220-245`): if group A
   groups by `{region, country}` but group B can only
   reach `{region}`, the `FULL JOIN` would fan out B's
   measures across the asymmetric dim. Hard error.
4. **Build per-group CTEs** (`:257-272`): one CTE per
   group, alias `f"{source_name}_agg"` (with collision
   suffixes), containing `SELECT dim_keys, agg(measures)
   FROM source JOIN ... GROUP BY dim_keys`.
5. **Outer SELECT** (`:278-281`): dimensions via
   `COALESCE(cte1.alias, cte2.alias, ...) AS alias`,
   measures via `cte_alias.measure_name`.
6. **Derived measures** (`:460-496`): wrap cross-CTE refs
   in `COALESCE(..., 0)` for non-divisors and
   `NULLIF(COALESCE(..., 0), 0)` for divisors. The
   divisor detection uses AST (`_find_divisor_deps`,
   `:499-511`) to find `exp.Div` nodes where the RHS is a
   measure-name column.
7. **CTE-to-CTE joins** (`:286-303`): `FULL JOIN` (or `JOIN`
   when `include_empty=False`) on shared dim keys. The
   first CTE is the LHS; subsequent CTEs are
   `COALESCE`-chained from the previous LHS, so the
   right-side equality is always anchored on a single
   representative.
8. **HAVING at outer** (`:305-318`): HAVING filters are
   *not* placed on the per-group CTEs (incorrect
   semantics across the FULL JOIN); they go on the outer
   `WHERE` after rewriting measure refs to
   `COALESCE(cte_alias.measure, 0)` (so `count(x) = 0`
   works against unmatched rows).
9. **ORDER BY / LIMIT** at the end.

**`_transpile`** (`:1400-1425`) is the one-line dialect
adaptation: read + write in `self.dialect` to preserve
dialect-specific user-authored exprs that were embedded in
the outer scaffold. Postgres is a no-op.

**Shared helpers worth lifting**:
- `_apply_measure_filter` (`:666-719`): injects
  `CASE WHEN filter THEN inner END` into each aggregate's
  argument via AST transform. Handles
  `count(*)` → `count(CASE WHEN ... THEN 1 END)`,
  `count(DISTINCT x)` → `count(DISTINCT CASE WHEN ... THEN
  x END)`, and the regular `sum(CASE WHEN ... THEN x END)`
  shape. **This is the implementation SemQL would need
  for the per-measure filter concept** — we don't have it
  today, but `Measure.filter` would be the natural
  addition.
- `_translate_custom_funcs` (`:721-763`): `count_distinct`
  → `COUNT(DISTINCT col)`, `percentile(p, col)` →
  `PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY col)`,
  `median(col)` → `PERCENTILE_CONT(0.5) WITHIN GROUP
  (ORDER BY col)`. Clean AST-based pattern.
- `_time_trunc` (`:858-910`) + `_sqlite_time_trunc` +
  `_mysql_time_trunc`: the per-dialect time-truncation
  logic. SemQL's `dialect.py` has a `trunc(granularity,
  field)` method (`compile.py:1480`); the ktx approach
  is more elaborate (sqlite uses `STRFTIME` + arithmetic,
  mysql uses `DATE_FORMAT` + `QUARTER()`, bigquery uses
  `DATE_TRUNC(field, GRANULARITY)`) but the shape is
  similar.
- `_dim_expr` (`:849-856`) + `_expand_computed_columns`
  (`:1286-1318`): computed columns are *expanded*, not
  referenced. SemQL handles computed columns differently
  (via `wrap_for_tenancy` + the cube's `with_ctes`),
  but the "expand at the boundary" discipline is the same.

**Cross-reference to SemQL.** The simple path (Path A) is
roughly equivalent to `compile.py:_emit_simple_query` +
`_CompileEnv.build_inner`. The locality path (Path B) is
**entirely new territory** for SemQL. The `_apply_measure_filter`
AST injection is the single most interesting concrete
function — it's the answer to "how do I support
`Measure.filter` without breaking aggregate semantics," and
we don't have it.

### Updated recommendation table

The original 11 candidates stand. Depth pass added:

| # | Lift | Effort | Status |
|---|------|--------|--------|
| 1 | Chasm-trap + aggregate-locality (planner + generator) | 600-800 LoC | **highest** |
| 2 | Two-tier loader | 1-2 days | unchanged |
| 3 | `Provenance` enum | < 1 day | unchanged |
| 4 | `ValidationReport` value type | < 1 day | unchanged |
| 5 | `engine.suggest()` (error-as-recovery-plan) | 1-2 days | unchanged |
| 6 | Steiner-tree + Dijkstra + ambiguity detection | < 1 day | unchanged |
| 7 | Reserved-identifier quoting | < 0.5 day | unchanged |
| 8 | Module-top "DIALECT CONVENTION" blocks | < 1 hour | unchanged |
| 9 | `lru_cache` on `(sql, dialect)` parse | < 1 hour | unchanged |
| 10 | Symmetric `from_sources()` factory | audit only | unchanged |
| 11 | Architectural lint as CI check | 0.5 day | unchanged |
| **12** | **`_inside_subquery` aggregate detection** | **< 0.5 day** | **new — pair with #4** |
| **13** | **`_apply_measure_filter` AST injection** | **< 0.5 day** | **new — unlocks per-Measure.filter** |
| **14** | **`MeasureGroup` as a structured value type** | **< 0.5 day** | **new — required by #1, valuable on its own** |
| **15** | **HAVING clause separation (`_classify_filters`)** | **1-2 days** | **new — required by #1, valuable on its own** |
| **16** | **Source CTE inner-WITH hoisting** | **< 1 day** | **new — robustness win** |
| **17** | **`_resolve_measure_dict` predefined-ref rewriting** | **< 0.5 day** | **new — closes a gap in `InlineDerived`** |

12 and 13 are tiny and have correctness value independent of
the chasm-trap work. 14, 15, 16, 17 fall out of 1 but each
is independently useful and could land as standalone PRs.

### Things that look borrowable but aren't

After the depth pass, the original anti-patterns list holds
and a few more candidates get *demoted*, not promoted:

- **k`_detect_fan_out` as a single 250-LoC function** — don't
  lift as-is. Refactor first.
- **The `__derived__` magic string** (`planner.py:407, 515`,
  `generator.py:454, 940, 1056, 1070`) — ktx uses
  `source_name="__derived__"` as a sentinel for "this
  measure is composed of other measures, not native to a
  source." SemQL's `InlineDerived` doesn't have this
  problem (it carries the operator), but if we ever add
  `Measure(agg="derived")` for catalog-declared derived
  measures, we should use a typed flag
  (`is_derived: bool`) or a discriminator, not a magic
  string.
- **The regex-fallback paths in `parser.py` /
  `generator.py`** — every "if AST fails, try regex" path
  logs at `logger.debug` and is documented as last-resort.
  These exist because real user SQL occasionally defeats
  sqlglot. **The pattern is fine, but the test surface
  for "the regex path returns the same answer as the AST
  path" is non-trivial.** If we borrow `quote_reserved_identifiers`
  or `_apply_measure_filter`, plan for an oracle test
  suite that compares the AST path to the regex path on
  a corpus of fixtures.

### Concrete first PRs (revised)

Given the depth pass, the order changes slightly:

1. **Cheap-wins batch** — Candidates 3, 4, 7, 8, 9, plus
   12, 13, 17. All < 0.5 day each. Single PR, ~1 day
   total. Closes correctness gaps in `InlineDerived`,
   reserved-word handling, parse caching, dialect-convention
   documentation, and adds `Provenance` + `ValidationReport`
   value types.
2. **`MeasureGroup` value type + HAVING classifier** (14
   + 15) — required by chasm-trap but independently
   valuable. Pair with 4 (warnings channel) so classifier
   issues are surfaced as warnings, not errors. ~2-3 days.
3. **Steiner-tree + Dijkstra + ambiguity** (6) — small,
   pure, drop-in to `logical.py:find_join_path`. ~1 day.
4. **Two-tier loader** (2) — touches the loader and the
   introspection package. ~2-3 days.
5. **Chasm-trap + aggregate-locality** (1) — open as a
   TDD spike in `packages/semql/`, port `_detect_fan_out`
   as three refactored functions, port the locality path
   into `generator.py`. ~1-2 weeks with snapshots.
6. **`engine.suggest()`** (5) — needs `find_components`
   (lift from `graph.py:207-242`) and the suggest-shape
   work. ~1-2 days. Defer until 1 and 4 are in.
7. **Architectural lint** (11) — 0.5 day. Independent of
   the rest. Could land first if desired.
8. **Source CTE inner-WITH hoisting** (16) — robustness
   win for `DerivedTable.sql` users. ~1 day. Independent.
