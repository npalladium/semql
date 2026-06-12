# Naming convention — query / plan / spec / compiled / decision

Status: proposed · June 2026 · companion to `naming-review-2026-06.md`
(which covers individually misleading names; this doc covers the
*system* of names). Pre-v1 is the rename window.

## 0. The problem, measured

Today "plan" means four unrelated things and "spec" two:

| Name | Module | What it actually is |
|---|---|---|
| `LogicalPlan` | logical.py | compiler-internal IR (live Cube refs, not serialisable) |
| `QueryPlan` / `QueryStep` | plan.py | **LLM pipeline output** — which queries to run to answer a question (headline/breakdown/compare) |
| `FederatedPlan` | federate.py | executor contract: fragments + merge instructions |
| `MergePlan` | federate.py | rendered merge **SQL + params** — an artifact, not a plan |
| `RowPlan` | entities spec (proposed) | versioned wire contract for row-mode reads |
| `spec.py` | module | the caller-facing **query request language** (SemanticQuery, Filter, TimeWindow…) |
| `MergeSpec` | federate.py | structured merge instructions — the actual *plan* for the merge |
| `CatalogSpec` | review B5 (proposed) | serialisable data-form of catalog definitions |

The sharpest symptom: `FederatedPlan.merge` (`MergePlan`, rendered SQL)
and `FederatedPlan.merge_spec` (`MergeSpec`, structured) are two
representations of one meaning with swapped names — the "Spec" is the
plan and the "Plan" is the compiled artifact. This is review B1's
dual-implementation problem surfacing in the vocabulary.

`plan.py` vs `logical.py` is the other trap: a reader looking for the
compiler's plan opens `plan.py` and finds prompt-pipeline types.

## 1. Option A — stage taxonomy (recommended)

Name by **pipeline stage**, which simultaneously encodes *who authors
it* and *what may consume it*. The grammar:

> A **Query** says *what* the caller wants. Authored by callers (human
> or LLM), validated against the catalog, serialisable.
>
> A **Plan** says *how* to get it. Authored by the compiler only —
> never by callers. A plan that crosses a process boundary carries a
> version; a plan that doesn't is internal and unexported.
>
> A **Compiled** artifact is finished SQL: text + bound params +
> output shape. Nothing downstream re-decides anything.
>
> A **Spec** is definition data: what the catalog author wrote, in
> serialisable form. (Reserved for exactly this — see §1.2.)
>
> A **Decision** is a surfaced inference: a choice plus its reasons
> ("no decision made silently").
>
> A **Result** is what came back from execution.

One sentence to rule future names:
**Query → compile → Plan → render → Compiled → execute → Result**,
and *the LLM pipeline plans answers, not queries.*

### 1.1 Rename table

| Current | New | Why |
|---|---|---|
| `QueryPlan` / `QueryStep` (plan.py) | `AnswerPlan` / `AnswerStep` | it plans the *answer* (headline/breakdown/compare), not query execution; kills the worst collision |
| `plan.py` (module) | `pipeline.py` (or `answer.py`) | it holds the four prompt-role outputs; "plan" belongs to the compiler |
| `MergeSpec` | `MergePlan` | structured instructions for the merge = the plan |
| `MergePlan` | `CompiledMerge` | SQL + params = artifact, exactly parallel to `CompiledQuery` |
| `FederatedPlan.merge` / `.merge_spec` | `.compiled_merge` / `.merge` | follows from the two above; one meaning, clearly two forms (structured + rendered) |
| `spec.py` (module) | `query.py` | it holds the request language; `SemanticQuery` living in `semql.query` is self-describing |
| `FederatedPlan` | keep name; make frozen + versioned | it *is* a plan and crosses to the engine package — must meet the plan contract |
| `LogicalPlan` | keep name; remove from `__all__` | internal plan; unexported per the contract (naming-review §6) |
| `RowPlan` (entities) | keep | already the model citizen: derived, versioned, wire-safe |
| `CatalogSpec` (B5, when built) | keep | the one legitimate "Spec" |
| `SemanticQuery`, `SavedQuery`, `CompiledQuery` | keep | already correct under the grammar |
| `RouterDecision`, `ParserDecision`, `VizDecision` | keep | consistent Decision family |
| `ResolvedQuery` (introspect.py) | keep (or `QueryResolution`) | it's an analysis *of* a query, not a query; rename only if it confuses in practice |
| `_PartitionPlan`, `_RawRowPartitionPlan` | keep | internal, already underscored |

### 1.2 The fate of "Spec"

Reserved for one meaning: **the serialisable data-form of authored
definitions** (`CatalogSpec`; an `EntitySpec` would qualify if entities
ever split data from behaviour). Everything else loses the word:
the request language is `query`, derived structures are `Plan`s.
If that reservation feels too subtle, the alternative is to retire
"Spec" entirely and name B5's type `CatalogData` — acceptable, but
"spec" for authored-definition-as-data is well-trodden (K8s
`spec:` vs `status:` is exactly this split).

### 1.3 Contract checklist per suffix (enforce in review, later in lint)

- `*Query` / `*Mutation`: Pydantic, frozen, serialisable, constructible
  from JSON by an LLM, validated by the catalog — never contains SQL.
- `*Plan`: frozen; authored by the compiler; if exported or consumed by
  another package → `version: int` + serialisable, else unexported.
- `Compiled*`: sql + params + output shape; immutable; no live objects.
- `*Spec`: pure data, no callables; round-trips through
  `model_dump`/`from_dict`.
- `*Decision`: carries the choice *and* the reasons/alternatives.
- `*Result`: execution output; never cached by reference (defect A5).

### 1.4 Pydantic vs dataclasses (the framework follows the stage)

The codebase already follows a coherent unwritten rule — Pydantic in
`model.py`/`spec.py`/`plan.py`/`rewrite.py`, dataclasses in
`logical.py`/`compile.py`/`federate.py`/`_resolve.py` — and even the
apparent contradictions resolve under it: `RouterDecision` is Pydantic
because an *LLM* fills it in (validation is the retry loop), while
`VizDecision`/`ParserDecision` are dataclasses because *library code*
computes them. The rule, now written:

> **Pydantic, frozen** — anything constructed from JSON by callers or
> LLMs, and anything that crosses a process boundary: the
> Query/Mutation families, Specs, LLM pipeline outputs, wire Plans
> (`RowPlan`, `FederatedPlan`), `Compiled*`.
>
> **`@dataclass(frozen=True, slots=True)`** — everything
> library-constructed and process-internal: IR nodes, resolutions,
> decisions, lint findings.
>
> Either way: tuples not lists, frozen always.

Do **not** unify on one framework. All-Pydantic taxes the compile hot
path and drags validation semantics into the IR; all-dataclass loses
boundary validation, which is load-bearing for the LLM workflow.
Validate where data enters the process; stay light where the compiler
talks to itself.

Nuance for library-*produced* wire types (`CompiledQuery`,
`FederatedPlan`): construction-time validation is pointless, but
serialisation and versioning are required — Pydantic still wins because
`model_construct()` skips validation on the hot path while keeping
`model_dump`/`model_validate` for the wire. `LogicalPlan` stays a
dataclass precisely because it is the one plan that never leaves the
process — the same fact that unexports it (§1.1).

Misplacements to fix (all on the architecture-review list already):

| Type | Today | Should be | Why |
|---|---|---|---|
| `CompiledQuery` | plain mutable dataclass | frozen Pydantic | the product artifact; crosses to engine/MCP/cache; mutability is adjacent to defect A5 |
| `FederatedPlan` | mutable dataclass | frozen Pydantic + `version` | crosses the package boundary; must meet the `*Plan` contract (§1.3) |
| frozen types with `list` fields | unhashable (review B10) | tuples | hashability is what frozen is *for* |

## 2. Option B — minimal renames, classification only

If churn must be near-zero: rename only the two outright collisions —

1. `QueryPlan`/`QueryStep` → `AnswerPlan`/`AnswerStep` (and ideally the
   `plan.py` module),
2. swap `MergeSpec`/`MergePlan` → `MergePlan`/`CompiledMerge`,

then add §1's grammar + checklist to PHILOSOPHY/CONTRIBUTING as the
rule for *future* names, leaving `spec.py`, `ResolvedQuery`, etc.
untouched. Cost: the module names keep lying (`spec.py` holds queries,
nothing in `plan.py` is a compiler plan), and the convention starts
with documented exceptions.

## 3. Option C — request/plan/artifact (rejected, recorded)

A more radical scheme renames the caller layer to `*Request`
(`QueryRequest`, `MutationRequest`). Rejected: `SemanticQuery` is the
project's brand-level noun (it's in the package name), and "Query =
caller intent" is already the strongest convention in the codebase —
the fix is to make everything else consistent *with* it, not to move
it.

## 4. Recommendation

Option A, sequenced:

1. The two collisions (B's list) — these fix active confusion. *(hours)*
2. Module renames `spec.py` → `query.py`, `plan.py` → `pipeline.py`,
   with deprecation re-export shims for one release. *(hours)*
3. `FederatedPlan` frozen + versioned; `LogicalPlan` unexported —
   already on the architecture-review list (B1, B5). *(with that work)*
4. Add §1.3's checklist to CONTRIBUTING; later, a lint rule that any
   exported `*Plan` has a `version` field. *(with the lint work)*

Success criterion: a new contributor can place any type in the pipeline
by its suffix alone, and `grep -l "Plan"` returns only files that
participate in compilation or execution.
