---
name: semql-requirement-discovery
description: >
  Discover what semantic-layer cubes a developer needs to build. Use
  upstream of `semql-cube`: when the user wants to bootstrap a SemQL
  catalog from a PRD, a feature brief, or a vague intent ("we want
  to answer revenue questions"), this skill interviews them and / or
  reads provided spec docs, then emits a structured requirements
  document the `semql-cube` skill consumes to write actual Cubes.
---

# Discovering catalog requirements

This skill bridges **intent** ("we want analytics for our orders
table") to **catalog structure** (cubes, measures, dimensions, joins,
auth). Its output is a markdown requirements document the
`semql-cube` skill reads to author the Python `Cube` definitions.

**You're in this skill when:**
- The user has not yet written cube code and is asking what to build.
- The user hands you a PRD / spec / Notion doc and says "model this."
- The user wants help breaking a vague analytics ask into concrete
  cubes / measures / dimensions.

**You're NOT in this skill when:**
- The user already knows the shape and wants the Python code written
  → use `semql-cube`.
- The user has cubes and wants to query them → use the
  `build_query_generator_prompt_fragment` surface in core.

## Inputs the skill consumes

Either or both:

1. **PRD documents** — the user names file paths or URLs to spec
   material (product briefs, feature designs, schema dumps). Read
   them with the `Read` tool. Cite specific sections in your follow-up
   questions so the user knows you're grounded.
2. **Interview answers** — when the PRD is missing, ambiguous, or
   silent on a needed dimension, ask. Use `AskUserQuestion` for
   structured multi-choice decisions; use plain prose for open-ended
   ones.

## The interview structure

Walk the developer through these in order. Skip questions the PRD
already answers; ask the rest.

### 1. Domain + business questions

Get the *questions users will ask*, not "the entities in our schema."
The schema falls out of the questions. Aim for 5-10 example questions
spanning headline, breakdown, compare, and context shapes.

Examples:
- "What was revenue last quarter?" → headline
- "Revenue by region for the same period?" → breakdown
- "How does that compare to the previous quarter?" → compare
- "How many orders contributed?" → context

If the user can't generate questions, that's a signal the analytics
ask is underspecified — flag it and ask them to come back.

### 2. Source data

For each question, identify the table(s) involved. Get:
- Backend (Postgres, ClickHouse, DuckDB, BigQuery, Snowflake)
- Table name (with `{schema}` placeholder if multi-tenant)
- DDL or column list (ask for `\d table_name` output or equivalent)

A cube ≈ a table. Cross-table aggregations are joins, not single
cubes.

### 3. Per cube: measures and dimensions

For each table:
- **Measures** — what gets aggregated? Default to `count(*)` + the
  one or two numeric columns the questions reference. Note each
  measure's `agg` (sum / count / count_distinct / avg / min / max /
  ratio) and `unit` (currency, count, duration, percent).
- **Dimensions** — what gets filtered or grouped on? Categoricals
  and IDs. Note each dimension's type (string / number / time / bool
  / uuid).
- **Time dimensions** — separate list. Note allowed granularities
  (hour / day / week / month) based on the underlying column.

### 4. Joins

For each pair of cubes a question crosses, capture:
- Direction (which side is many, which is one)
- The join predicate (`{a}.col = {b}.col`)
- Whether it should be auto-derived from a `foreign_key` on a
  dimension (preferred when natural)

### 5. Reusable predicates and required filters

Centralise repeated WHERE clauses:
- **Segments** — named predicates a planner references by name
  ("paid_orders", "active_users").
- **`required_filters`** — dimensions a query MUST filter on (tenant
  ID, status). The compiler refuses queries that omit them.

### 6. Authorisation

Critical and easy to miss. Ask:
- **Who sees what?** A `viewer` is the request's identity
  (`AuthContext { viewer_id, roles, metadata }`). Roles drive the
  static `Cube.required_roles` ANY-match.
- **Row-level scoping?** Does any cube need rows filtered by viewer
  identity ("my tickets," "my team's orders")? If yes, identify the
  scope as a `ScopeFn` candidate — name it, describe the predicate.
- **Tenant model?** SCHEMA (per-tenant database schema),
  DISCRIMINATOR (shared table, tenant column), or NONE.

### 7. Views (curated facades)

When the catalog grows past ~10 cubes, ask whether the planner should
see a curated subset for common question shapes. Each view names a
handful of `cube.field` references under view-local aliases.

## Output format

Write the requirements as a markdown document. Suggested path:
`docs/requirements/<catalog_name>.md`. Use this structure verbatim
so `semql-cube` can parse it predictably:

```markdown
# Catalog Requirements: <name>

## Context
<1-2 paragraphs on the domain, audience, and primary question shapes>

## Cubes

### <cube_name>
- **Backend**: postgres | clickhouse | duckdb | bigquery | snowflake
- **Table**: `<table_expression, e.g. {schema}.orders>`
- **Alias**: `<one or two letters>`
- **Description**: <one sentence>
- **Primary key**: <dimension_name>  *(optional)*
- **Measures**:
  - `<name>` — `agg=<agg>`, `unit=<unit>`, sql `<{alias}.col>`,
    description: <one line>
- **Dimensions**:
  - `<name>` — `type=<type>`, sql `<{alias}.col>`, foreign_key:
    `<other_cube>` *(optional)*
- **Time dimensions**:
  - `<name>` — granularities=<list>, sql `<{alias}.col>`
- **Segments** *(optional)*:
  - `<name>` — sql `<{alias}.predicate>`
- **Joins**:
  - many_to_one → `<other_cube>` on `{a}.col = {b}.col`
- **Required filters** *(optional)*: `[dim1, dim2]`
- **Required roles** *(optional)*: `[role1, role2]` (ANY-match)
- **Scope** *(optional)*: `<scope_fn_name>`
- **Tenancy**: schema | discriminator | none

### <next_cube_name>
...

## Views *(optional)*

### <view_name>
- **Description**: <one line>
- **Fields**:
  - `<local_name>` → `<cube.field>`

## Authorisation

- **Expected viewer roles**: `[role1, role2, role3]`
- **Scope functions**:
  - `<name>` — <one-line description of who sees what>
    - Predicate sketch: `<sql with {alias} and {ctx.X} placeholders>`
    - Required ctx keys: `[ctx.X, ctx.Y]`

## Open questions

- <Anything the interview / PRD didn't resolve. semql-cube will
  surface these before writing any Cube that depends on them.>
```

## Output report

After writing the requirements doc, send the user a short report:

1. Path to the requirements document.
2. Cube count + view count.
3. Any **open questions** you couldn't resolve from the PRD /
   interview — call these out as blockers.
4. Suggested next step: invoke `semql-cube` (or its slash command)
   pointing at the requirements doc.

End with: "Want me to dig into any of these — refine a cube, draft
the SQL fragments for a measure, or sketch the ScopeFn?" This is the
follow-up invitation — the user opts in to interactive refinement,
opts out by doing nothing.

## Interview etiquette

- One bundle of related questions at a time, not one at a time. Use
  `AskUserQuestion` for closed choices (4 options max).
- Cite the PRD when you have it: "Section 3.2 says transactions are
  USD-denominated — should `revenue.unit` be `currency`?"
- Don't invent column names. If the user gave you "amount" and you
  need "tax_rate," ask.
- Don't write Python in this skill. Output is a markdown spec.
- When the user gives a one-line answer like "yes" or "no," do
  follow-up to fill in the implications. "Yes, scope to reportees"
  → who's the manager, what's the org table, what's the recursion?

## Common pitfalls

- **Sketching the schema before the questions are clear.** If the
  user can't list 5 questions, the catalog is premature.
- **Inventing measures from column names.** A column named
  `is_active` isn't a measure — it's a bool dimension. Measures are
  what you'd put `SUM()` / `COUNT()` around.
- **Skipping auth.** Every new cube should pass through Section 6.
  Auth bolted on after-the-fact is where bugs land.
- **One giant cube.** A "facts" table that holds orders + customers
  + products inline is a cube smell. Split by entity; declare joins.

## See also

- `skills/semql-cube.md` — the downstream skill that authors Cubes
  from the requirements doc this skill produces.
- `docs/decisions.md` — design decisions worth pinning ("we don't
  use PyYAML for catalogs," etc.).
- `PHILOSOPHY.md` — the catalog-is-data + auth-is-compiler-side
  invariants the requirements doc must honour.
