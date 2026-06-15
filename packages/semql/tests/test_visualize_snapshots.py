# mypy: disable-error-code=type-arg
# pyright: reportMissingTypeArgument=false, reportUnknownParameterType=false, reportUnknownArgumentType=false
"""Snapshot tests for the full ``VizDecision`` output.

The point tests in ``test_visualize.py`` cover the decision table at
the branch level — each branch picks the right chart, the right
boundary fires at the right off-by-one. These snapshots pin the
*presentation* surface: the per-column metadata, the title, the
axis labels, the structured ``DecisionReason``. They're the test
that catches the things the branch tests can't: a typo in
``_humanize`` that turns "Created At" into "Created at", a silent
regression in the format-inference table, a renamed reason ``kind``
that the audit surface would silently mis-classify.

The expectation is: update deliberately, review the diff in
``tests/__snapshots__/`` before accepting.
"""

from __future__ import annotations

from semql import (
    Catalog,
    CompareWindow,
    Cube,
    Dialect,
    Dimension,
    Measure,
    SemanticQuery,
    ShapeStats,
    TimeDimension,
    TimeWindow,
    decide_visualization,
)
from syrupy.assertion import SnapshotAssertion


def _orders_catalog() -> Catalog:
    return Catalog(
        [
            Cube(
                name="orders",
                dialect=Dialect.POSTGRES,
                table="orders",
                alias="o",
                description="Order lines.",
                display_name="Orders",
                measures=[
                    Measure(
                        name="revenue",
                        sql="{o}.amount",
                        agg="sum",
                        unit="currency",
                        display_name="Net Revenue",
                    ),
                    Measure(name="count", sql="*", agg="count", unit="count"),
                ],
                dimensions=[
                    Dimension(
                        name="region",
                        sql="{o}.region",
                        type="string",
                        display_name="Sales Region",
                    ),
                    Dimension(
                        name="status",
                        sql="{o}.status",
                        type="string",
                    ),
                ],
                time_dimensions=[
                    TimeDimension(
                        name="created_at",
                        sql="{o}.created_at",
                        granularities=("day", "week", "month"),
                    ),
                ],
            )
        ]
    )


def _decision(
    q: SemanticQuery,
    n_rows: int,
    *,
    shape_stats: ShapeStats | None = None,
) -> object:
    cat = _orders_catalog()
    compiled = cat.compile(q)
    return decide_visualization(
        q,
        compiled,
        n_rows=n_rows,
        catalog=cat.as_dict(),
        shape_stats=shape_stats,
    )


def test_pie_decision_full_shape(snapshot: SnapshotAssertion) -> None:
    """Small 1-dim 1-measure breakdown: pie, no ShapeStats override."""
    decision = _decision(
        SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        n_rows=3,
    )
    assert decision == snapshot


def test_bar_decision_full_shape(snapshot: SnapshotAssertion) -> None:
    """1-dim 1-measure medium breakdown: bar, full column metadata."""
    decision = _decision(
        SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        n_rows=15,
    )
    assert decision == snapshot


def test_compare_decision_full_shape(snapshot: SnapshotAssertion) -> None:
    """Compare query: compare_line_chart with all four per-measure
    facets as y_axes, kind=compare_current_prior reason."""
    decision = _decision(
        SemanticQuery(
            measures=["orders.revenue"],
            time_dimension=TimeWindow(
                dimension="orders.created_at",
                granularity="month",
                range=("2026-01-01", "2026-04-01"),
            ),
            compare=CompareWindow(mode="previous_period"),
        ),
        n_rows=3,
    )
    assert decision == snapshot


def test_shape_stats_fallback_full_shape(snapshot: SnapshotAssertion) -> None:
    """ShapeStats(negatives=True) downgrades pie→bar and records the
    rejected pick in ``alternatives``."""
    decision = _decision(
        SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        n_rows=3,
        shape_stats=ShapeStats(has_negatives=True),
    )
    assert decision == snapshot


def test_time_series_full_shape(snapshot: SnapshotAssertion) -> None:
    """Time series: line_chart with time-column x-axis."""
    decision = _decision(
        SemanticQuery(
            measures=["orders.revenue"],
            time_dimension=TimeWindow(
                dimension="orders.created_at",
                granularity="day",
                range=("2026-01-01", "2026-02-01"),
            ),
        ),
        n_rows=31,
    )
    assert decision == snapshot
