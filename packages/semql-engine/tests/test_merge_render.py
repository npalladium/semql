"""``semql_engine.merge.render_merge_sql`` golden tests.

The expected strings were frozen at F-M2, where each was verified
byte-for-byte equal to the core federation emitter's output (params
included) before that emitter was deleted in F-M3. They now serve two
ends: a regression net on the renderer itself, and — because they were
captured against the pre-removal core SQL — proof that the F-M3
extraction of spec-building out of the SQL emitter left the
``MergeSpec`` unchanged (a changed spec would render different SQL).

The degenerate single-backend plan is the one exception: core emitted a
literal ``SELECT * FROM frag_0`` there, while the spec renderer emits an
explicit passthrough projection. That case is checked for shape, not
byte-equality, and verified semantically by the engine execution tests.
"""

from __future__ import annotations

import pytest
from semql import (
    Catalog,
    Cube,
    Dialect,
    Dimension,
    Join,
    Measure,
    SemanticQuery,
    TimeDimension,
)
from semql.federate import FederationMode, compile_federated_query
from semql.spec import BoolExpr, Filter, TimeWindow
from semql.spec import SemanticQuery as SQ
from semql_engine.merge import render_merge_sql


def _orders(dialect: Dialect = Dialect.POSTGRES) -> Cube:
    return Cube(
        name="orders",
        dialect=dialect,
        table="orders",
        alias="o",
        primary_key="id",
        measures=[
            Measure(name="revenue", sql="{o}.amount", agg="sum", unit="currency"),
            Measure(name="count", sql="*", agg="count", unit="count"),
            Measure(name="avg_amount", sql="{o}.amount", agg="avg", unit="currency"),
            Measure(name="distinct_customers", sql="{o}.customer_id", agg="count_distinct"),
            Measure(name="min_amount", sql="{o}.amount", agg="min"),
            Measure(name="max_amount", sql="{o}.amount", agg="max"),
            Measure(name="amount_median", sql="{o}.amount", agg="median"),
            Measure(name="aov", sql="", agg="ratio", numerator="revenue", denominator="count"),
        ],
        dimensions=[
            Dimension(name="id", sql="{o}.id", type="number"),
            Dimension(
                name="customer_id", sql="{o}.customer_id", type="number", foreign_key="customers"
            ),
            Dimension(name="status", sql="{o}.status", type="string"),
        ],
        time_dimensions=[TimeDimension(name="created_at", sql="{o}.created_at")],
        joins=[Join(to="customers", relationship="many_to_one", on="{o}.customer_id = {c}.id")],
    )


def _customers(dialect: Dialect = Dialect.BIGQUERY) -> Cube:
    return Cube(
        name="customers",
        dialect=dialect,
        table="customers",
        alias="c",
        primary_key="id",
        dimensions=[
            Dimension(name="id", sql="{c}.id", type="number"),
            Dimension(name="region", sql="{c}.region", type="string"),
            Dimension(name="tier", sql="{c}.tier", type="string"),
        ],
    )


def _cat() -> dict[str, Cube]:
    return Catalog([_orders(), _customers()]).as_dict()


_DAY = TimeWindow(
    dimension="orders.created_at", granularity="day", range=("2020-01-01", "2020-02-01")
)
_REGION = ["customers.region"]

# (label, query, mode) cases that exercise a real multi-backend merge.
_CASES: list[tuple[str, SemanticQuery, FederationMode]] = [
    ("distributive_sum", SQ(measures=["orders.revenue"], dimensions=_REGION), "distributive"),
    ("distributive_count", SQ(measures=["orders.count"], dimensions=_REGION), "distributive"),
    ("distributive_avg", SQ(measures=["orders.avg_amount"], dimensions=_REGION), "distributive"),
    (
        "distributive_time_day",
        SQ(measures=["orders.revenue"], dimensions=["customers.region"], time_dimension=_DAY),
        "distributive",
    ),
    (
        "distributive_multi_measure",
        SQ(measures=["orders.revenue", "orders.count"], dimensions=["customers.region"]),
        "distributive",
    ),
    (
        "distributive_order_limit",
        SQ(
            measures=["orders.revenue"],
            dimensions=["customers.region"],
            order=[("orders.revenue", "desc")],
            limit=10,
            offset=5,
        ),
        "distributive",
    ),
    (
        "distributive_cross_partition_where",
        SQ(
            measures=["orders.revenue"],
            dimensions=["customers.region"],
            where=BoolExpr(
                op="or",
                children=[
                    Filter(dimension="orders.status", op="eq", values=["shipped"]),
                    Filter(dimension="customers.tier", op="eq", values=["gold"]),
                ],
            ),
        ),
        "distributive",
    ),
    (
        "raw_count_distinct",
        SQ(measures=["orders.distinct_customers"], dimensions=_REGION),
        "raw_rows",
    ),
    ("raw_min", SQ(measures=["orders.min_amount"], dimensions=_REGION), "raw_rows"),
    ("raw_max", SQ(measures=["orders.max_amount"], dimensions=_REGION), "raw_rows"),
    ("raw_count_star", SQ(measures=["orders.count"], dimensions=_REGION), "raw_rows"),
    ("raw_percentile", SQ(measures=["orders.amount_median"], dimensions=_REGION), "raw_rows"),
    ("raw_ratio", SQ(measures=["orders.aov"], dimensions=_REGION), "raw_rows"),
    (
        "raw_time_day",
        SQ(measures=["orders.revenue"], dimensions=["customers.region"], time_dimension=_DAY),
        "raw_rows",
    ),
    (
        "raw_having",
        SQ(
            measures=["orders.distinct_customers"],
            dimensions=["customers.region"],
            having=[Filter(dimension="orders.distinct_customers", op="gt", values=[5])],
        ),
        "raw_rows",
    ),
]


# Frozen at F-M2 (== core emission at that point). label -> (sql, params).
_J = ' FROM frag_0 AS f0 LEFT JOIN frag_1 AS f1 ON "f0"."customer_id" = "f1"."id"'
_EXPECTED: dict[str, tuple[str, dict[str, object]]] = {
    "distributive_sum": (
        f'SELECT "f1"."region" AS "region", SUM("f0"."revenue") AS "revenue"{_J} GROUP BY 1',
        {},
    ),
    "distributive_count": (
        f'SELECT "f1"."region" AS "region", SUM("f0"."count") AS "count"{_J} GROUP BY 1',
        {},
    ),
    "distributive_avg": (
        'SELECT "f1"."region" AS "region", SUM("f0"."avg_amount__avg_sum") / '
        'NULLIF(SUM("f0"."avg_amount__avg_count"), 0) AS "avg_amount"' + _J + " GROUP BY 1",
        {},
    ),
    "distributive_time_day": (
        'SELECT "f1"."region" AS "region", "f0"."created_at_day" AS "created_at_day", '
        'SUM("f0"."revenue") AS "revenue"' + _J + " GROUP BY 1, 2",
        {},
    ),
    "distributive_multi_measure": (
        'SELECT "f1"."region" AS "region", SUM("f0"."revenue") AS "revenue", '
        'SUM("f0"."count") AS "count"' + _J + " GROUP BY 1",
        {},
    ),
    "distributive_order_limit": (
        f'SELECT "f1"."region" AS "region", SUM("f0"."revenue") AS "revenue"{_J} '
        'GROUP BY 1 ORDER BY "revenue" DESC LIMIT 10 OFFSET 5',
        {},
    ),
    "distributive_cross_partition_where": (
        f'SELECT "f1"."region" AS "region", SUM("f0"."revenue") AS "revenue"{_J} '
        'WHERE "f0"."status" = $m0 OR "f1"."tier" = $m1 GROUP BY 1',
        {"m0": "shipped", "m1": "gold"},
    ),
    "raw_count_distinct": (
        'SELECT "f1"."region" AS "region", COUNT(DISTINCT "f0"."__rm_distinct_customers") '
        'AS "distinct_customers"' + _J + " GROUP BY 1",
        {},
    ),
    "raw_min": (
        f'SELECT "f1"."region" AS "region", MIN("f0"."__rm_min_amount") AS "min_amount"{_J} '
        "GROUP BY 1",
        {},
    ),
    "raw_max": (
        f'SELECT "f1"."region" AS "region", MAX("f0"."__rm_max_amount") AS "max_amount"{_J} '
        "GROUP BY 1",
        {},
    ),
    "raw_count_star": (
        f'SELECT "f1"."region" AS "region", COUNT(*) AS "count"{_J} GROUP BY 1',
        {},
    ),
    "raw_percentile": (
        'SELECT "f1"."region" AS "region", PERCENTILE_CONT(0.5 ORDER BY '
        '"f0"."__rm_amount_median") AS "amount_median"' + _J + " GROUP BY 1",
        {},
    ),
    "raw_ratio": (
        'SELECT "f1"."region" AS "region", SUM("f0"."__rm_revenue") / NULLIF(COUNT(*), 0) '
        'AS "aov"' + _J + " GROUP BY 1",
        {},
    ),
    "raw_time_day": (
        'SELECT "f1"."region" AS "region", DATE_TRUNC(\'day\', "f0"."__rt_created_at") '
        'AS "created_at_day", SUM("f0"."__rm_revenue") AS "revenue"' + _J + " GROUP BY 1, 2",
        {},
    ),
    "raw_having": (
        'SELECT "f1"."region" AS "region", COUNT(DISTINCT "f0"."__rm_distinct_customers") '
        'AS "distinct_customers"' + _J + ' GROUP BY 1 HAVING "distinct_customers" > $m0',
        {"m0": 5},
    ),
}


@pytest.mark.parametrize("label,query,mode", _CASES, ids=[c[0] for c in _CASES])
def test_render_matches_golden(label: str, query: SemanticQuery, mode: FederationMode) -> None:
    plan = compile_federated_query(query, _cat(), mode=mode)
    assert len(plan.fragments) == 2, f"{label}: expected a real two-backend merge"
    sql, params = render_merge_sql(plan.merge_spec)
    expected_sql, expected_params = _EXPECTED[label]
    assert sql == expected_sql, f"{label}: rendered SQL drifted from frozen golden"
    assert params == expected_params, f"{label}: rendered params drifted"


def test_single_backend_renders_passthrough_projection() -> None:
    """The degenerate single-backend spec renders an explicit passthrough
    SELECT over frag_0 (not the literal ``SELECT * FROM frag_0`` core uses
    for that case) — no aggregation, every output column projected."""
    standalone = Cube(
        name="orders",
        dialect=Dialect.POSTGRES,
        table="orders",
        alias="o",
        primary_key="id",
        measures=[Measure(name="revenue", sql="{o}.amount", agg="sum", unit="currency")],
        dimensions=[
            Dimension(name="id", sql="{o}.id", type="number"),
            Dimension(name="status", sql="{o}.status", type="string"),
        ],
    )
    plan = compile_federated_query(
        SemanticQuery(measures=["orders.revenue"], dimensions=["orders.status"]),
        Catalog([standalone]).as_dict(),
    )
    assert len(plan.fragments) == 1
    sql, params = render_merge_sql(plan.merge_spec)
    assert sql.upper().startswith("SELECT")
    assert "frag_0" in sql
    assert "GROUP BY" not in sql.upper()
    assert params == {}
