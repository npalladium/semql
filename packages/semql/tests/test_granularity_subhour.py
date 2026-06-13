"""R3 — sub-hour granularity (minute / second).

Extends the time-bucket grain below ``hour``. ``date_trunc('minute', …)``
/ ``date_trunc('second', …)`` are native on every supported dialect;
ClickHouse uses ``toStartOfMinute`` / ``toStartOfSecond``.

A ``time`` TimeDimension permits the sub-hour grains by default; a
``date`` TimeDimension refuses them (a calendar date has no
time-of-day to truncate) — the same rule that already rejects
``hour`` on a date.

Arbitrary N-unit buckets (15-minute / 5-second) are out of scope for
this pass.
"""

from __future__ import annotations

import pytest
from semql.model import Cube, Dialect, Measure, TimeDimension
from semql.spec import SemanticQuery, TimeWindow

# ---------------------------------------------------------------------------
# Spec accepts the new grains
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grain", ["minute", "second"])
def test_time_window_accepts_subhour_grain(grain: str) -> None:
    tw = TimeWindow(
        dimension="orders.created_at",
        granularity=grain,  # type: ignore[arg-type]
        range=("2026-01-01", "2026-01-02"),
    )
    assert tw.granularity == grain


# ---------------------------------------------------------------------------
# TimeDimension allow-list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grain", ["minute", "second"])
def test_time_dim_default_permits_subhour(grain: str) -> None:
    td = TimeDimension(name="created_at", sql="{o}.created_at")
    assert grain in td.granularities


@pytest.mark.parametrize("grain", ["minute", "second"])
def test_date_dim_default_excludes_subhour(grain: str) -> None:
    td = TimeDimension(name="day", sql="{o}.day", type="date")
    assert grain not in td.granularities


@pytest.mark.parametrize("grain", ["hour", "minute", "second"])
def test_date_dim_refuses_explicit_subhour(grain: str) -> None:
    with pytest.raises(ValueError, match="calendar date|time-of-day"):
        TimeDimension(name="day", sql="{o}.day", type="date", granularities=(grain,))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Compilation — date_trunc emission
# ---------------------------------------------------------------------------


def _cube(dialect: Dialect) -> Cube:
    return Cube(
        name="orders",
        dialect=dialect,
        table="public.orders",
        alias="o",
        measures=[Measure(name="revenue", sql="{o}.amount", agg="sum", unit="currency")],
        dimensions=[],
        time_dimensions=[TimeDimension(name="created_at", sql="{o}.created_at")],
    )


def _bucketed_query(grain: str) -> SemanticQuery:
    return SemanticQuery(
        measures=["orders.revenue"],
        time_dimension=TimeWindow(
            dimension="orders.created_at",
            granularity=grain,  # type: ignore[arg-type]
            range=("2026-01-01T00:00:00", "2026-01-01T01:00:00"),
        ),
    )


@pytest.mark.parametrize("grain", ["minute", "second"])
def test_postgres_emits_subhour_date_trunc(grain: str) -> None:
    from semql.compile import compile_query

    out = compile_query(_bucketed_query(grain), {"orders": _cube(Dialect.POSTGRES)})
    assert f"date_trunc('{grain}'" in out.sql.lower()
    # The bucket column is named after the grain.
    assert f"created_at_{grain}" in out.sql


@pytest.mark.parametrize("grain", ["minute", "second"])
def test_clickhouse_emits_tostartof_subhour(grain: str) -> None:
    from semql.compile import compile_query

    out = compile_query(_bucketed_query(grain), {"orders": _cube(Dialect.CLICKHOUSE)})
    expected = {"minute": "toStartOfMinute", "second": "toStartOfSecond"}[grain]
    assert expected in out.sql


def test_first_class_dialect_transpiles_subhour() -> None:
    from semql.compile import compile_query

    out = compile_query(_bucketed_query("minute"), {"orders": _cube(Dialect.TRINO)})
    assert "DATE_TRUNC('MINUTE'" in out.sql.upper()
