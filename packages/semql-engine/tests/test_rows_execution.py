"""M3 — row-mode execution.

The golden contract: the SQL path (a DuckDB adapter running
``compiled.sql``) and the plan path (the reference ``InMemoryRowAdapter``
interpreting ``compiled.plan``) return *identical records* for the same
``EntityFetch`` / ``EntityList``. Scope (discriminator tenancy) is honored
on both.
"""

from __future__ import annotations

import duckdb
import pytest
from semql import (
    AuthContext,
    Catalog,
    Cube,
    Dimension,
    Entity,
    EntityFetch,
    EntityList,
    Measure,
)
from semql.model import Dialect
from semql_engine import AdapterResult, DuckDBAdapter, InMemoryRowAdapter, execute_entity

# Shared seed data — the same rows feed both the DuckDB table and the
# in-memory adapter, keyed identically.
_ROWS = [
    {"id": 1, "region": "us", "status": "open", "org_id": "acme"},
    {"id": 2, "region": "eu", "status": "closed", "org_id": "acme"},
    {"id": 3, "region": "us", "status": "open", "org_id": "globex"},
    {"id": 4, "region": "us", "status": "closed", "org_id": "acme"},
]


def _cube(**kw: object) -> Cube:
    return Cube(
        name="orders",
        dialect=Dialect.DUCKDB,
        table="orders",
        alias="o",
        measures=[Measure(name="revenue", sql="{o}.id", agg="count", unit="count")],
        dimensions=[
            Dimension(name="id", sql="{o}.id", type="number"),
            Dimension(name="region", sql="{o}.region", type="string"),
            Dimension(name="status", sql="{o}.status", type="string"),
        ],
        primary_key="id",
        **kw,  # type: ignore[arg-type]
    )


def _entity(**kw: object) -> Entity:
    return Entity(
        name="order",
        cubes=["orders"],
        key="orders.id",
        list_filters=["orders.region", "orders.status"],
        default_order="orders.id asc",
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture
def duck_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE orders (id INTEGER, region TEXT, status TEXT, org_id TEXT)")
    for r in _ROWS:
        con.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?)",
            [r["id"], r["region"], r["status"], r["org_id"]],
        )
    return con


def _records(result: AdapterResult) -> list[dict[str, object]]:
    cols = result.columns
    return [dict(zip(cols, row, strict=True)) for row in result.rows]


# ---------------------------------------------------------------------------
# Golden: SQL path == plan path
# ---------------------------------------------------------------------------


def test_fetch_sql_and_plan_paths_identical(duck_con: duckdb.DuckDBPyConnection) -> None:
    cat = Catalog([_cube()], entities=[_entity()])
    sql_compiled = cat.fetch(EntityFetch(entity="order", key=2))
    sql_records = _records(execute_entity(sql_compiled, DuckDBAdapter(duck_con)))

    # Same entity, but custom-backend so the plan path is exercised.
    cat2 = Catalog([_cube()], entities=[_entity(custom_backend=True)])
    plan_compiled = cat2.fetch(EntityFetch(entity="order", key=2))
    assert plan_compiled.sql is None
    plan_records = _records(execute_entity(plan_compiled, InMemoryRowAdapter({"orders": _ROWS})))

    assert sql_records == plan_records
    assert sql_records == [{"id": 2, "region": "eu", "status": "closed"}]


def test_list_sql_and_plan_paths_identical(duck_con: duckdb.DuckDBPyConnection) -> None:
    cat = Catalog([_cube()], entities=[_entity()])
    sql_compiled = cat.list_rows(EntityList(entity="order", where={"orders.status": "open"}))
    sql_records = _records(execute_entity(sql_compiled, DuckDBAdapter(duck_con)))

    cat2 = Catalog([_cube()], entities=[_entity(custom_backend=True)])
    plan_compiled = cat2.list_rows(EntityList(entity="order", where={"orders.status": "open"}))
    plan_records = _records(execute_entity(plan_compiled, InMemoryRowAdapter({"orders": _ROWS})))

    assert sql_records == plan_records
    # status=open, ordered by id asc → rows 1 and 3.
    assert [r["id"] for r in sql_records] == [1, 3]


def test_list_in_filter_identical(duck_con: duckdb.DuckDBPyConnection) -> None:
    cat = Catalog([_cube()], entities=[_entity()])
    spec = EntityList(entity="order", where={"orders.region": ["eu", "us"]}, limit=2)
    sql_records = _records(execute_entity(cat.list_rows(spec), DuckDBAdapter(duck_con)))

    cat2 = Catalog([_cube()], entities=[_entity(custom_backend=True)])
    plan_records = _records(
        execute_entity(cat2.list_rows(spec), InMemoryRowAdapter({"orders": _ROWS}))
    )
    assert sql_records == plan_records
    assert len(sql_records) == 2  # limit honored on both


# ---------------------------------------------------------------------------
# Scope honored on both paths (discriminator tenancy)
# ---------------------------------------------------------------------------


def test_scope_honored_on_both_paths(duck_con: duckdb.DuckDBPyConnection) -> None:
    viewer = AuthContext(viewer_id="u1", tenant="acme")

    sql_cat = Catalog(
        [_cube(tenancy="discriminator", tenancy_columns=["org_id"])],
        entities=[_entity()],
    )
    sql_records = _records(
        execute_entity(
            sql_cat.list_rows(EntityList(entity="order"), viewer=viewer),
            DuckDBAdapter(duck_con),
        )
    )

    plan_cat = Catalog(
        [_cube(tenancy="discriminator", tenancy_columns=["org_id"])],
        entities=[_entity(custom_backend=True)],
    )
    plan_records = _records(
        execute_entity(
            plan_cat.list_rows(EntityList(entity="order"), viewer=viewer),
            InMemoryRowAdapter({"orders": _ROWS}),
        )
    )

    assert sql_records == plan_records
    # Only acme rows (1, 2, 4) — globex row 3 is scoped out on both paths.
    assert sorted(str(r["id"]) for r in sql_records) == ["1", "2", "4"]


# ---------------------------------------------------------------------------
# Dispatch errors
# ---------------------------------------------------------------------------


def test_execute_entity_rejects_wrong_adapter_for_sql(duck_con: duckdb.DuckDBPyConnection) -> None:
    cat = Catalog([_cube()], entities=[_entity()])
    compiled = cat.fetch(EntityFetch(entity="order", key=1))  # has SQL
    with pytest.raises(TypeError, match=r"(?i)row|execute"):
        execute_entity(compiled, InMemoryRowAdapter({"orders": _ROWS}))


def test_execute_entity_rejects_sql_adapter_for_custom(
    duck_con: duckdb.DuckDBPyConnection,
) -> None:
    cat = Catalog([_cube()], entities=[_entity(custom_backend=True)])
    compiled = cat.fetch(EntityFetch(entity="order", key=1))  # sql is None
    with pytest.raises(TypeError, match=r"(?i)RowCapable|execute_rows"):
        execute_entity(compiled, DuckDBAdapter(duck_con))
