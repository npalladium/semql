"""Temporal model.

Covers the four pieces of the temporal-model work:
1. quarter / year granularity (per-dialect trunc)
2. temporal value validation at construction (TimeWindow.range, time filters)
3. per-cube timezone threaded into truncation
4. DATE-vs-TIMESTAMP type distinction
"""

from __future__ import annotations

import pytest
from semql import (
    Cube,
    Dimension,
    Measure,
    SemanticQuery,
    TimeDimension,
    TimeWindow,
    compile_query,
)
from semql.backend import (
    ClickHouseDialect,
    DuckDBDialect,
    PostgresDialect,
    render,
)
from semql.model import Dialect, GranularityLiteral
from sqlglot import exp

# ---------------------------------------------------------------------------
# 1. quarter / year granularity
# ---------------------------------------------------------------------------


def test_granularity_literal_includes_quarter_and_year() -> None:
    from typing import get_args

    assert set(get_args(GranularityLiteral)) == {
        "hour",
        "day",
        "week",
        "month",
        "quarter",
        "year",
    }


@pytest.mark.parametrize("granularity", ["quarter", "year"])
def test_std_dialect_trunc_supports_quarter_year(granularity: str) -> None:
    s = PostgresDialect()
    out = render(s.trunc(granularity, exp.column("ts", table="x")), Dialect.POSTGRES)
    assert "date_trunc" in out.lower()
    assert granularity in out.lower()


def test_clickhouse_trunc_quarter_year_uses_native_functions() -> None:
    s = ClickHouseDialect()
    assert "toStartOfQuarter" in render(
        s.trunc("quarter", exp.column("ts", table="x")), Dialect.CLICKHOUSE
    )
    assert "toStartOfYear" in render(
        s.trunc("year", exp.column("ts", table="x")), Dialect.CLICKHOUSE
    )


def test_duckdb_trunc_year() -> None:
    s = DuckDBDialect()
    out = render(s.trunc("year", exp.column("ts", table="x")), Dialect.DUCKDB)
    assert "date_trunc" in out.lower() and "year" in out.lower()


def _orders_cube() -> Cube:
    return Cube(
        name="orders",
        dialect=Dialect.POSTGRES,
        table="orders",
        alias="o",
        measures=[Measure(name="revenue", sql="{o}.amount", agg="sum")],
        time_dimensions=[TimeDimension(name="placed_at", sql="{o}.placed_at")],
    )


@pytest.mark.parametrize("granularity", ["quarter", "year"])
def test_compile_query_groups_by_quarter_year(granularity: str) -> None:
    cube = _orders_cube()
    q = SemanticQuery(
        measures=["orders.revenue"],
        time_dimension=TimeWindow(
            dimension="orders.placed_at",
            granularity=granularity,  # type: ignore[arg-type]
            range=("2024-01-01", "2025-01-01"),
        ),
    )
    out = compile_query(q, {"orders": cube}, context={})
    assert f"date_trunc('{granularity}'" in out.sql.lower().replace('"', "")


# ---------------------------------------------------------------------------
# 2. temporal value validation at construction
# ---------------------------------------------------------------------------


def test_timewindow_rejects_reversed_iso_range() -> None:
    from pydantic import ValidationError as PydValidationError

    with pytest.raises(PydValidationError, match=r"(?i)reversed|start.*after|before"):
        TimeWindow(dimension="orders.placed_at", range=("2025-01-01", "2024-01-01"))


def test_timewindow_allows_equal_endpoints() -> None:
    # Half-open [t, t) is empty but legal.
    tw = TimeWindow(dimension="orders.placed_at", range=("2026-01-01", "2026-01-01"))
    assert tw.range == ("2026-01-01", "2026-01-01")


def test_timewindow_allows_ordered_range() -> None:
    tw = TimeWindow(dimension="orders.placed_at", range=("2024-01-01", "2025-01-01"))
    assert tw.range[0] < tw.range[1]


def test_timewindow_accepts_non_iso_values_for_binding() -> None:
    # Security model: arbitrary strings are parameter-bound downstream, not
    # rejected here. Only well-formed instants get the ordering guard.
    tw = TimeWindow(dimension="orders.placed_at", range=("not-a-date", "also-bad"))
    assert tw.range == ("not-a-date", "also-bad")


# ---------------------------------------------------------------------------
# 3. per-cube timezone threaded into truncation
# ---------------------------------------------------------------------------


def test_cube_accepts_timezone() -> None:
    cube = Cube(
        name="orders",
        dialect=Dialect.POSTGRES,
        table="orders",
        alias="o",
        timezone="America/New_York",
        dimensions=[Dimension(name="region", sql="{o}.region", type="string")],
    )
    assert cube.timezone == "America/New_York"


def test_std_trunc_with_timezone_emits_at_time_zone() -> None:
    s = PostgresDialect()
    out = render(
        s.trunc("day", exp.column("ts", table="x"), timezone="America/New_York"),
        Dialect.POSTGRES,
    )
    assert "AT TIME ZONE 'America/New_York'" in out


def test_clickhouse_trunc_with_timezone_passes_tz_arg() -> None:
    s = ClickHouseDialect()
    out = render(
        s.trunc("day", exp.column("ts", table="x"), timezone="Europe/Berlin"),
        Dialect.CLICKHOUSE,
    )
    assert "toStartOfDay" in out and "Europe/Berlin" in out


def test_trunc_without_timezone_is_unchanged() -> None:
    s = PostgresDialect()
    out = render(s.trunc("day", exp.column("ts", table="x")), Dialect.POSTGRES)
    assert "AT TIME ZONE" not in out


def test_compile_query_threads_cube_timezone() -> None:
    cube = Cube(
        name="orders",
        dialect=Dialect.POSTGRES,
        table="orders",
        alias="o",
        timezone="America/New_York",
        measures=[Measure(name="revenue", sql="{o}.amount", agg="sum")],
        time_dimensions=[TimeDimension(name="placed_at", sql="{o}.placed_at")],
    )
    q = SemanticQuery(
        measures=["orders.revenue"],
        time_dimension=TimeWindow(
            dimension="orders.placed_at",
            granularity="day",
            range=("2024-01-01", "2025-01-01"),
        ),
    )
    out = compile_query(q, {"orders": cube}, context={})
    assert "AT TIME ZONE 'America/New_York'" in out.sql


# ---------------------------------------------------------------------------
# 4. DATE-vs-TIMESTAMP distinction
# ---------------------------------------------------------------------------


def test_dim_type_literal_includes_date() -> None:
    from typing import get_args

    from semql.model import DimTypeLiteral

    assert "date" in get_args(DimTypeLiteral)


def test_dimension_accepts_date_type() -> None:
    d = Dimension(name="signup_day", sql="{o}.signup_day", type="date")
    assert d.type == "date"


def test_date_time_dimension_default_granularities_exclude_hour() -> None:
    td = TimeDimension(name="signup_day", sql="{o}.signup_day", type="date")
    assert "hour" not in td.granularities
    assert "day" in td.granularities


def test_date_time_dimension_rejects_hour_granularity() -> None:
    from pydantic import ValidationError as PydValidationError

    with pytest.raises(PydValidationError, match=r"(?i)date.*hour|hour.*date"):
        TimeDimension(
            name="signup_day",
            sql="{o}.signup_day",
            type="date",
            granularities=("hour", "day"),
        )


def test_date_dim_truncation_skips_timezone() -> None:
    # A DATE column has no time-of-day / zone — truncation must not emit a
    # timezone shift even when the cube declares one.
    cube = Cube(
        name="orders",
        dialect=Dialect.POSTGRES,
        table="orders",
        alias="o",
        timezone="America/New_York",
        measures=[Measure(name="revenue", sql="{o}.amount", agg="sum")],
        time_dimensions=[TimeDimension(name="signup_day", sql="{o}.signup_day", type="date")],
    )
    q = SemanticQuery(
        measures=["orders.revenue"],
        time_dimension=TimeWindow(
            dimension="orders.signup_day",
            granularity="month",
            range=("2024-01-01", "2025-01-01"),
        ),
    )
    out = compile_query(q, {"orders": cube}, context={})
    assert "AT TIME ZONE" not in out.sql
    assert "date_trunc('month'" in out.sql.lower().replace('"', "")
