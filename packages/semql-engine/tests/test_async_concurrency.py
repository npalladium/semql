"""Concurrent AsyncEngine calls must not corrupt each other.

The merge step materialises fragments into fixed ``frag_<i>`` tables. A
single shared DuckDB connection across in-flight ``run()`` / ``iter_run()``
coroutines (the normal FastAPI fan-out) would race on those tables —
one call's reset/load clobbering another's mid-stream. The engine gives
each call its own isolated connection; these tests pin that invariant by
running many overlapping calls and checking every result is correct.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from typing import Any

import duckdb
import pytest
from semql import (
    Cube,
    Dialect,
    Dimension,
    Join,
    Measure,
    SemanticQuery,
    compile_federated_query,
)
from semql.federate import FederatedPlan
from semql.spec import Filter
from semql_engine import AdapterResult, AsyncEngine


def _run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # type: ignore[arg-type]


class _SlowAdapter:
    """DuckDB-backed adapter that sleeps before returning, forcing the
    two concurrent calls' fragment fetches + merges to interleave."""

    def __init__(self, con: duckdb.DuckDBPyConnection, delay: float) -> None:
        self._con = con
        self._delay = delay

    async def execute(self, sql: str, params: Mapping[str, Any]) -> AdapterResult:
        await asyncio.sleep(self._delay)
        cur = self._con.execute(sql, dict(params))
        return AdapterResult(columns=[d[0] for d in cur.description], rows=cur.fetchall())


def _orders() -> Cube:
    return Cube(
        name="orders",
        dialect=Dialect.DUCKDB,
        table="orders",
        alias="o",
        primary_key="id",
        measures=[Measure(name="revenue", sql="{o}.amount", agg="sum", unit="currency")],
        dimensions=[
            Dimension(name="id", sql="{o}.id", type="number"),
            Dimension(
                name="customer_id", sql="{o}.customer_id", type="number", foreign_key="customers"
            ),
        ],
        joins=[Join(to="customers", relationship="many_to_one", on="{o}.customer_id = {c}.id")],
    )


def _customers() -> Cube:
    return Cube(
        name="customers",
        dialect=Dialect.DUCKDB,
        table="customers",
        alias="c",
        primary_key="id",
        dimensions=[
            Dimension(name="id", sql="{c}.id", type="number"),
            Dimension(name="region", sql="{c}.region", type="string"),
        ],
    )


@pytest.fixture()
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount DOUBLE)")
    c.execute(
        "INSERT INTO orders VALUES "
        "(1, 10, 100.0), (2, 10, 200.0), (3, 11, 50.0), (4, 12, 300.0), (5, 12, 25.0)"
    )
    c.execute("CREATE TABLE customers (id INTEGER, region TEXT)")
    c.execute("INSERT INTO customers VALUES (10, 'EU'), (11, 'US'), (12, 'EU')")
    return c


def _plan_for_customer(customer_id: int) -> FederatedPlan:
    catalog = {c.name: c for c in (_orders(), _customers())}
    q = SemanticQuery(
        measures=["orders.revenue"],
        dimensions=["customers.region"],
        filters=[Filter(dimension="orders.customer_id", op="eq", values=[customer_id])],
    )
    # Force a real two-fragment DuckDB merge (not the single-fragment fast path).
    return compile_federated_query(q, catalog)


# Revenue per customer (single region each): 10→EU 300, 11→US 50, 12→EU 325.
_EXPECTED = {10: [("EU", 300.0)], 11: [("US", 50.0)], 12: [("EU", 325.0)]}


def test_concurrent_run_calls_are_isolated(con: duckdb.DuckDBPyConnection) -> None:
    engine = AsyncEngine()
    engine.register(Dialect.DUCKDB, _SlowAdapter(con, delay=0.02))

    async def drive() -> list[tuple[int, list[tuple[Any, ...]]]]:
        cids = [10, 11, 12] * 4  # 12 overlapping calls
        plans = [_plan_for_customer(cid) for cid in cids]
        results = await asyncio.gather(*(engine.run(p) for p in plans))
        return [(cid, [tuple(r) for r in res.rows]) for cid, res in zip(cids, results, strict=True)]

    for cid, rows in _run(drive()):
        assert sorted(rows) == sorted(_EXPECTED[cid]), f"customer {cid} got {rows}"


def test_concurrent_iter_run_streams_are_isolated(con: duckdb.DuckDBPyConnection) -> None:
    """iter_run holds a merge cursor across await points; concurrent
    streams must not see each other's fragment tables."""
    engine = AsyncEngine()
    engine.register(Dialect.DUCKDB, _SlowAdapter(con, delay=0.02))

    async def collect(cid: int) -> list[tuple[Any, ...]]:
        out: list[tuple[Any, ...]] = []
        async for chunk in engine.iter_run(_plan_for_customer(cid), chunk_rows=1):
            out.extend(chunk)
        return out

    async def drive() -> list[tuple[int, list[tuple[Any, ...]]]]:
        cids = [10, 11, 12] * 4
        results = await asyncio.gather(*(collect(cid) for cid in cids))
        return list(zip(cids, results, strict=True))

    for cid, rows in _run(drive()):
        assert sorted(rows) == sorted(_EXPECTED[cid]), f"customer {cid} got {rows}"
