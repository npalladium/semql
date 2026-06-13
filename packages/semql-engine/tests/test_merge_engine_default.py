"""F-M3: ``DuckDBMergeEngine`` is a real MergeEngine that renders the
spec and executes it, and the engine's built-in inline merge now warns
(deprecated) while producing identical results.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping

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
from semql_engine import AdapterResult, DuckDBMergeEngine, Engine


def _orders() -> Cube:
    return Cube(
        name="orders",
        dialect=Dialect.POSTGRES,
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
        dialect=Dialect.BIGQUERY,
        table="customers",
        alias="c",
        primary_key="id",
        dimensions=[
            Dimension(name="id", sql="{c}.id", type="number"),
            Dimension(name="region", sql="{c}.region", type="string"),
        ],
    )


class _Adapter:
    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con

    def execute(self, sql: str, params: Mapping[str, object]) -> AdapterResult:
        cur = self._con.execute(sql, dict(params))
        return AdapterResult(columns=[d[0] for d in cur.description], rows=cur.fetchall())


def _adapters() -> dict[Dialect, _Adapter]:
    pg = duckdb.connect(":memory:")
    pg.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount DOUBLE)")
    pg.execute("INSERT INTO orders VALUES (1, 10, 100.0), (2, 11, 50.0), (3, 10, 25.0)")
    bq = duckdb.connect(":memory:")
    bq.execute("CREATE TABLE customers (id INTEGER, region TEXT)")
    bq.execute("INSERT INTO customers VALUES (10, 'EU'), (11, 'US')")
    return {Dialect.POSTGRES: _Adapter(pg), Dialect.BIGQUERY: _Adapter(bq)}


def _plan() -> object:
    catalog = {c.name: c for c in (_orders(), _customers())}
    return compile_federated_query(
        SemanticQuery(measures=["orders.revenue"], dimensions=["customers.region"]),
        catalog,
    )


def _engine(merge_engine: object | None) -> Engine:
    eng = Engine(merge_engine=merge_engine)  # type: ignore[arg-type]
    for dialect, adapter in _adapters().items():
        eng.register(dialect, adapter)
    return eng


def test_duckdb_merge_engine_renders_and_executes() -> None:
    """A DuckDBMergeEngine merge produces the joined + re-aggregated rows
    and emits no deprecation warning (it's the supported path)."""
    plan = _plan()
    engine = _engine(DuckDBMergeEngine())
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        result = engine.run(plan)  # type: ignore[arg-type]
    assert result.columns == ["region", "revenue"]
    assert {r[0]: r[1] for r in result.rows} == {"EU": 125.0, "US": 50.0}


def test_inline_merge_matches_duckdb_merge_engine() -> None:
    """The deprecated inline path yields identical results."""
    inline = _engine(None).run(_plan())  # type: ignore[arg-type]
    structured = _engine(DuckDBMergeEngine()).run(_plan())  # type: ignore[arg-type]
    assert {r[0]: r[1] for r in inline.rows} == {r[0]: r[1] for r in structured.rows}


def test_inline_merge_warns_deprecated() -> None:
    engine = _engine(None)
    with pytest.warns(DeprecationWarning, match="inline DuckDB merge is deprecated"):
        engine.run(_plan())  # type: ignore[arg-type]


def test_inline_merge_warns_only_once_per_engine() -> None:
    engine = _engine(None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        engine.run(_plan())  # type: ignore[arg-type]
        engine.run(_plan())  # type: ignore[arg-type]
    inline_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(inline_warnings) == 1
