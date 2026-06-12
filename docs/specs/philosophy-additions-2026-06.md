# Philosophy additions — June 2026

Proposed additions to `PHILOSOPHY.md`, drawn from the architecture
review: each is a principle that, had it been written, would have
prevented a defect the review actually found (refs A1–A6, B1–B10 in
`architecture-review-2026-06.md`).

The pattern: the current philosophy is strong on *what the compiler
promises outward* (auth, errors, SQL quality) and thin on *how the
codebase keeps itself honest inward* (no parallel implementations, no
silent narrowing, enforced metadata). The review's defects nearly all
live in the second category — items 1, 2, 4, 7 are the load-bearing
ones.

## Proposed text

### 1. Refusal over omission *(would have prevented A1, A3)*

> What the compiler does not understand, it refuses.
> Skipping an unsupported operator is a wrong result with extra steps.

### 2. One meaning, one implementation *(B1: parallel federate compiler, two CNFs, two alias handlers; B7: sync/async drift)*

> Every semantic decision is made in exactly one place.
> Where two implementations of one meaning must temporarily exist,
> a test forces them to agree — or one of them is deleted.

### 3. Boundaries are decided once *(A2)*

> All ranges are half-open: `[start, end)`. Everywhere, no exceptions.
> Time is parsed at the boundary and compared as time, never as strings.

### 4. A name is a promise *(B4; `is_safe_select`; `Backend` — see naming-review)*

> A field the compiler does not enforce, a name stronger than its check —
> these are documentation pretending to be code.
> Model only what is load-bearing; name only what is true.

### 5. The trust boundary, stated *(implicit today; the security programme in `test-plan.md` §8 needs it written)*

> Catalog authors are trusted — they already write SQL.
> Query values, viewers, and MCP clients are not.
> Untrusted input binds as parameters and meets typed refusals;
> it never shapes SQL text.

### 6. Capabilities are declared, not assumed *(A3, B7; the entities adapter contract)*

> An adapter or merge engine declares what it supports.
> The planner routes only to declared capabilities and refuses the rest.

### 7. Invariants are executable *(ties PHILOSOPHY.md to `test-plan.md`'s definition of done)*

> Every claim in this document names the test that enforces it.
> A principle without a failing test is an aspiration.

### 8. Wire formats are versioned *(generalises the RowPlan precedent; supports the existing "serialisable and versioned" catalog line)*

> Anything that crosses a process boundary carries a version
> and refuses versions it does not know.

## One existing line to revisit

**"Silence implies safety"** (PHILOSOPHY.md line 47) is aspirational
and currently inverted — raw SQL is the *default* at nine entry points
(review B2), so today silence implies raw SQL. The sentence is also
ambiguous in English (readable as permission to stay silent). Proposed
replacement, same intent, checkable:

> If SemQL has not flagged raw SQL, there is none.
