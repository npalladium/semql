"""R4 — configurable week-start (calendar config, week-start only).

A per-cube ``week_start`` (``monday`` default, or ``sunday``) controls
where ``week`` truncation begins. ``date_trunc('week', …)`` is
Monday-native on Postgres / DuckDB / Redshift / Trino / Databricks, so
Sunday-start is emitted with the standard ``+1 day / -1 day`` shift
around the native trunc. ClickHouse uses ``toStartOfWeek``'s explicit
mode argument (``1`` = Monday, ``0`` = Sunday).

Default ``monday`` keeps every dialect consistent: it also pins
ClickHouse to ``toStartOfWeek(t, 1)`` (the bare ``toStartOfWeek(t)``
defaulted to Sunday, out of step with the others).

Fiscal-year offset and ISO week numbering are out of scope for this
pass.
"""

from __future__ import annotations

import duckdb
import pytest
from semql.backend import (
    ClickHouseDialect,
    PostgresDialect,
    TrinoDialect,
    render,
)
from semql.compile import compile_query
from semql.model import Cube, Dialect, Measure, TimeDimension
from semql.spec import SemanticQuery, TimeWindow
from sqlglot import exp

# ---------------------------------------------------------------------------
# Cube model
# ---------------------------------------------------------------------------


def test_cube_week_start_defaults_to_monday() -> None:
    c = Cube(name="orders", dialect=Dialect.POSTGRES, table="t", alias="o")
    assert c.week_start == "monday"


def test_cube_rejects_unknown_week_start() -> None:
    with pytest.raises(ValueError, match="week_start"):
        Cube(
            name="orders",
            dialect=Dialect.POSTGRES,
            table="t",
            alias="o",
            week_start="friday",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# trunc() rendering — week-start aware
# ---------------------------------------------------------------------------


def test_postgres_week_monday_is_plain_date_trunc() -> None:
    s = PostgresDialect()
    expr = exp.column("ts", table="x")
    # Default monday: no shift, identical to today's output.
    assert render(s.trunc("week", expr), Dialect.POSTGRES) == "date_trunc('week', x.ts)"
    assert (
        render(s.trunc("week", expr, week_start="monday"), Dialect.POSTGRES)
        == "date_trunc('week', x.ts)"
    )


def test_postgres_week_sunday_uses_shift() -> None:
    s = PostgresDialect()
    expr = exp.column("ts", table="x")
    out = render(s.trunc("week", expr, week_start="sunday"), Dialect.POSTGRES)
    assert "date_trunc('week'" in out
    assert "INTERVAL '1 DAY'" in out
    # Shift forward then back: + 1 day inside the trunc, - 1 day outside.
    assert "+ INTERVAL '1 DAY'" in out and out.rstrip().endswith("- INTERVAL '1 DAY'")


def test_week_start_only_affects_week_grain() -> None:
    s = PostgresDialect()
    expr = exp.column("ts", table="x")
    for grain in ("day", "month", "quarter", "year"):
        monday = render(s.trunc(grain, expr, week_start="monday"), Dialect.POSTGRES)
        sunday = render(s.trunc(grain, expr, week_start="sunday"), Dialect.POSTGRES)
        assert monday == sunday == f"date_trunc('{grain}', x.ts)"


def test_clickhouse_week_mode_arg() -> None:
    s = ClickHouseDialect()
    expr = exp.column("ts", table="x")
    assert (
        render(s.trunc("week", expr, week_start="monday"), Dialect.CLICKHOUSE)
        == "toStartOfWeek(x.ts, 1)"
    )
    assert (
        render(s.trunc("week", expr, week_start="sunday"), Dialect.CLICKHOUSE)
        == "toStartOfWeek(x.ts, 0)"
    )


def test_transpiling_dialect_week_sunday_shift() -> None:
    s = TrinoDialect()
    expr = exp.column("ts", table="x")
    monday = render(s.trunc("week", expr, week_start="monday"), Dialect.TRINO)
    sunday = render(s.trunc("week", expr, week_start="sunday"), Dialect.TRINO)
    assert "DATE_TRUNC('WEEK'" in monday.upper()
    assert "INTERVAL" in sunday.upper() and "DATE_TRUNC('WEEK'" in sunday.upper()


# ---------------------------------------------------------------------------
# End-to-end compile
# ---------------------------------------------------------------------------


def _cube(week_start: str) -> Cube:
    return Cube(
        name="orders",
        dialect=Dialect.POSTGRES,
        table="public.orders",
        alias="o",
        week_start=week_start,  # type: ignore[arg-type]
        measures=[Measure(name="revenue", sql="{o}.amount", agg="sum", unit="currency")],
        time_dimensions=[TimeDimension(name="created_at", sql="{o}.created_at")],
    )


def _week_query() -> SemanticQuery:
    return SemanticQuery(
        measures=["orders.revenue"],
        time_dimension=TimeWindow(
            dimension="orders.created_at",
            granularity="week",
            range=("2026-01-01", "2026-02-01"),
        ),
    )


def test_end_to_end_sunday_week_shifts() -> None:
    out = compile_query(_week_query(), {"orders": _cube("sunday")})
    assert "INTERVAL '1 DAY'" in out.sql
    assert "date_trunc('week'" in out.sql.lower()


def test_end_to_end_monday_week_unshifted() -> None:
    out = compile_query(_week_query(), {"orders": _cube("monday")})
    assert "INTERVAL '1 DAY'" not in out.sql
    assert "date_trunc('week'" in out.sql.lower()


# ---------------------------------------------------------------------------
# Correctness — execute the rendered trunc against DuckDB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ts", "week_start", "expected"),
    [
        # 2026-01-11 is a Sunday; 2026-01-17 the following Saturday.
        ("2026-01-11", "monday", "2026-01-05"),  # Mon of that week
        ("2026-01-17", "monday", "2026-01-12"),  # Sat -> Mon 01-12
        ("2026-01-11", "sunday", "2026-01-11"),  # Sun -> itself
        ("2026-01-17", "sunday", "2026-01-11"),  # Sat -> Sun 01-11
        ("2026-01-12", "sunday", "2026-01-11"),  # Mon -> prior Sun
    ],
)
def test_duckdb_week_start_correctness(ts: str, week_start: str, expected: str) -> None:
    from semql.backend import DuckDBDialect

    s = DuckDBDialect()
    expr = exp.cast(exp.Literal.string(ts), "TIMESTAMP")
    sql = render(s.trunc("week", expr, week_start=week_start), Dialect.DUCKDB)  # type: ignore[arg-type]
    got = duckdb.sql(f"SELECT CAST(({sql}) AS DATE)").fetchone()
    assert got is not None
    assert str(got[0]) == expected, f"{ts} ({week_start}) -> {got[0]}, want {expected}"
