---
name: semql-cube
description: >
  Author a SemQL semantic cube or extend an existing catalog.
  Use when the user asks to model a table, add a cube, define
  dimensions / measures / time dimensions / segments / joins, declare
  a tenancy model, or register anything with the SemQL catalog.
---

# Authoring SemQL cubes

A **cube** is one logical table the planner can query. It declares
where the rows live (`backend`, `table`), an always-on membership
predicate (`base_predicate`), the *measures* / *dimensions* /
*time dimensions* / *segments* exposed, and the *joins* to other
cubes.

A **catalog** is a validated collection of cubes plus a thin
convenience surface (`compile`, `prompt`, `as_dict`).

## Step 1: pick the backend

```python
from semql import Backend

Backend.POSTGRES     # %name placeholder, native ILIKE
Backend.CLICKHOUSE   # {name:Type} placeholder, toStartOf<Gran>
Backend.DUCKDB       # $name placeholder, native ILIKE
Backend.BIGQUERY     # @name placeholder, ILIKE → LOWER LIKE LOWER
Backend.SNOWFLAKE    # :name placeholder, native ILIKE
Backend.META         # reflection cubes — see `META_CUBES`
```

Cross-backend queries are rejected at compile time. If one query
needs columns from two backends, split it or wait for the federation
TODO to land.

## Step 2: declare the cube

```python
from semql import Cube, Dimension, Measure, TimeDimension, Backend

orders = Cube(
    name="orders",                    # qualified-reference root: orders.X
    backend=Backend.POSTGRES,
    table="public.orders",            # may contain {schema}, {tenant_schema}
    alias="o",                        # what the SQL FROM clause uses
    base_predicate="{o}.deleted_at IS NULL",
    description="One row per checkout, paid or not.",
    measures=[
        Measure(name="count", sql="*", agg="count", unit="count"),
        Measure(
            name="revenue",
            sql="{o}.amount",
            agg="sum",
            unit="currency",
            format="currency",
        ),
    ],
    dimensions=[
        Dimension(name="region", sql="{o}.region", type="string"),
        Dimension(name="amount", sql="{o}.amount", type="number"),
    ],
    time_dimensions=[
        TimeDimension(
            name="created_at",
            sql="{o}.created_at",
            granularities=("hour", "day", "week", "month"),
        ),
    ],
)
```

### The `{alias}` placeholder

Every `sql` fragment uses `{<alias>}` (or `{<cube_name>}`, both
resolve to the cube's alias). The compiler substitutes them at
compile time so the emitted SQL is always alias-qualified. Other
known placeholders:

- `{schema}` / `{tenant_schema}` — caller-supplied via the `context`
  kwarg on `Catalog.compile(...)`.
- `{ctx.X}` (inside `security_sql` only) — bound as a parameter, not
  inlined as a SQL literal.

## Step 3: pick aggregation / dimension type

| `Measure.agg` | Use when |
|---|---|
| `sum`             | adds (revenue, count of events) |
| `count`           | row count; `sql="*"` is fine |
| `count_distinct`  | unique cardinality — sets `non_additive=True` semantics |
| `avg` / `min` / `max` | obvious |

| `Dimension.type` | Behaviour |
|---|---|
| `string`  | default; filter values must be strings |
| `number`  | numeric comparisons (`gt`/`lt`/...) |
| `time`    | filter values must parse as ISO-8601 |
| `bool`    | filter values must be Python `bool` |
| `uuid`    | filter values must parse as UUIDs |

Time dimensions go in `time_dimensions=` rather than `dimensions=`
because the compiler can truncate them with `granularity`.

## Step 4: declare joins

A `Join` is a directed edge. The BFS finds a path; multiple edges
can compose.

```python
from semql import Join

orders = Cube(
    ...,
    joins=[
        Join(
            to="customers",
            relationship="many_to_one",  # one_to_one | one_to_many | many_to_one
            on="{o}.customer_id = {customers}.id",
        ),
    ],
)
```

## Step 5: reusable predicates and required filters

```python
from semql import Segment

orders = Cube(
    ...,
    segments=[
        Segment(
            name="paid",
            sql="{o}.status = 'paid'",
            description="Confirmed payment received.",
        ),
    ],
    # Dimensions a query MUST filter on (any op, any value).
    required_filters=["region"],
)
```

A query then references `segments=["orders.paid"]` to apply the
predicate without re-deriving it.

## Step 6: tenancy + row-level security

```python
orders = Cube(
    ...,
    tenancy="discriminator",          # schema | discriminator | none
    tenancy_column="tenant_id",       # required for discriminator
    security_sql="{o}.user_id = {ctx.user_id}",
)
```

`schema` substitutes `{tenant_schema}` in the table name.
`discriminator` wraps the FROM in `SELECT * FROM ... WHERE
tenant_col = bind(tenant)` *inside* the alias — an outer OR predicate
can't smuggle in cross-tenant rows. `security_sql` AND-composes with
tenancy in the same subquery.

## Step 7: register with a Catalog

```python
from semql import Catalog

catalog = Catalog([orders, customers])
```

`Catalog([...])` validates on construction: no duplicate cube names,
every `Join.to` resolves, every `required_filter` names a real
dimension. META cubes (`catalog_cubes` / `catalog_measures` /
`catalog_dimensions`) auto-append.

## Step 8: query

```python
from semql import SemanticQuery, Filter, TimeWindow

compiled = catalog.compile(
    SemanticQuery(
        measures=["orders.revenue"],
        dimensions=["orders.region"],
        time_dimension=TimeWindow(
            dimension="orders.created_at",
            granularity="day",
            range=("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
        ),
        filters=[Filter(dimension="orders.region", op="in", values=["us", "ca"])],
        segments=["orders.paid"],
        order=[("revenue", "desc")],
        limit=100,
    ),
    context={"tenant": "acme"},   # for tenancy='discriminator' / {schema}
)
print(compiled.sql, compiled.params)
```

For OR / NOT predicates, use `where=BoolExpr(op="or", children=[...])`
instead of (or alongside) `filters`.

## Step 9: surface to LLMs

```python
print(catalog.prompt())                  # planner-facing fragment
print(catalog.prompt(include_introspection=True))  # + META cubes
```

The fragment teaches the LLM the catalog's vocabulary and the
`SemanticQuery` shape so it emits valid specs your code compiles.

## Common pitfalls

- **Forgetting the `{alias}` placeholder** — `sql="amount"` won't
  compile; use `sql="{o}.amount"` so the column qualifies in joined
  queries.
- **Aggregating in `Measure.sql`** — let `agg=` do it. `sql="{o}.amount"`
  + `agg="sum"` emits `SUM(o.amount)`. Don't pre-wrap.
- **Required filter not in `filters`** — putting it inside a `where`
  tree's `or` branch doesn't satisfy `required_filters` because that
  branch isn't AND-only.
- **Bare measure name in `having`** — both `having=[Filter(dimension="revenue",
  ...)]` and `having=[Filter(dimension="orders.revenue", ...)]` resolve;
  the bare form looks up by alias.
- **Granularity not declared on the time dimension** — if a query
  asks for `granularity="hour"` but the dim only declares
  `granularities=("day", "month")`, compile fails.

## See also

- `docs/api/semql.md` — auto-generated API reference for every public
  symbol. Run `uv run scripts/gen_api_docs.py` to refresh.
- `PHILOSOPHY.md` — design invariants. Catalogues are *data*; the
  emitted SQL is the product; structure carries the meaning.
- The existing test fixtures under `packages/semql/tests/` are the
  best living examples of well-shaped cubes.
