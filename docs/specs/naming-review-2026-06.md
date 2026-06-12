# Naming review — June 2026

Misleading names found during the architecture review, worst first.
Pre-v1 is the rename window ("break freely before v1").
Companion: `architecture-review-2026-06.md` (defect refs A1–A6, B1–B10),
`philosophy-additions-2026-06.md` (the principles these violate).

## 1. `compile_plan` (compile.py)

The name promises "emit SQL *from this plan*"; the implementation
reverse-engineers a SemanticQuery from the plan and recompiles — which
is exactly how it drops filters (defect A1). The byte-equality test
trusted the name. The fix is behavioural (make it actually compile the
plan); until then the name is actively lying to readers.

## 2. `is_safe_select` (safe.py, public API)

"Safe" reads as a security guarantee; it's a syntactic shape check
(single read-only SELECT). The property tests use it as an oracle,
which inflates what they prove. **Rename** to `is_single_select` or
`is_read_only_statement`. Spend the word "safe" only where the auth
machinery backs it.

## 3. `Backend` enum (`Cube.backend`)

It's a *dialect*. The engine tests prove it: in-process DuckDB "posing
as Postgres" works precisely because `Backend.POSTGRES` only controls
SQL emission — the real backend is the adapter. The protocol name
`BackendDialect` contains the confusion verbatim. **Rename** the enum
to `Dialect`, the field to `Cube.dialect`; "backend" then unambiguously
means the adapter/execution side in semql-engine.

## 4. `TimeWindow.range`

Documented inclusive (`spec.py:42-44`), but the name carries Python's
half-open connotation, and `partition.py` treats it half-open
(defect A2). Whichever convention is pinned (test plan says half-open,
everywhere), the field name must be impossible to misread — `range` is
right *iff* half-open wins.

## 5. `Join.relationship`

A field whose name reads as "the compiler knows the cardinality", but
nothing consumes it (review B4) — a one_to_many join under `sum`
silently inflates. Metadata the compiler doesn't enforce is
documentation pretending to be code. Wire the fan-out guard, or mark
the field advisory in its docstring until then.

## 6. `LogicalPlan` exported in `__all__`

Public placement says "stable IR contract"; the object carries live
`Cube` references and cannot cross a process boundary. Either it
becomes the real serialisable IR or it leaves the public surface. The
*placement* is the misleading name.

## 7. `apply_partition_to_plan` / `apply_rollup_to_plan`

Named like production transforms; they are test-only, while production
uses parallel inline mechanisms (review B1). A reader extending
partitioning will modify the function production never calls.

## 8. `AsyncEngine`

The name promises "Engine, but async"; it lacks the P7 cache and the
`on_execute` hook (review B7). Parity-implying names need parity — or a
docstring stating the delta.

## 9. `security_sql`

"Security" + unchecked raw SQL string. The name describes intent
honestly, but this is where "when raw SQL is used, SemQL says so" is
most violated by silence. Rename to `scope_sql` only if "security"
should be reserved for checked constructs; otherwise flag it in
`lint_catalog`.

## 10. Minor

- `MergeEngine` implies interchangeable implementations honouring one
  contract — defect A3 shows they don't. Contract gap, not a rename;
  the capability-declaration work (B7) fixes the lie.
- `ungrouped` is mechanism-naming, not intent-naming ("row mode") — but
  it matches Cube.dev's term. Keep; record as deliberate.

## Disposition

| Name | Action |
|---|---|
| `compile_plan` | fix behaviour (A1); name then becomes true |
| `is_safe_select` | rename: `is_read_only_statement` |
| `Backend` | rename: `Dialect` / `Cube.dialect` |
| `TimeWindow.range` | pin half-open convention; name then fits |
| `Join.relationship` | enforce (fan-out guard) or docstring "advisory" |
| `LogicalPlan` export | remove from `__all__` until serialisable |
| `apply_*_to_plan` | make production-path (B1) or move into tests |
| `AsyncEngine` | reach parity (B7) or document delta |
| `security_sql` | lint-flag; rename optional |
