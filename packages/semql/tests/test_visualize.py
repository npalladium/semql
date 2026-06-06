"""Unit tests for ``semql.visualize``.

The decision table inside ``_pick_chart_type`` is the only place
SemQL says "use a pie chart" / "use a line chart" / "use a data table".
The branches are short but the *boundaries* (``PIE_MAX_SLICES``,
``BAR_MAX_BARS``) and the conflict-resolution behaviour
(multiple cubes with different ``default_chart_type``) are the things
that drift silently when someone re-orders the if-chain.
"""

from __future__ import annotations

import pytest
from semql.model import Backend, Cube, Dimension, Measure, TimeDimension
from semql.spec import SemanticQuery, TimeWindow
from semql.visualize import (
    BAR_MAX_BARS,
    PIE_MAX_SLICES,
    VizColumn,
    decide_visualization,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _orders(default_chart_type: object | None = None) -> Cube:
    kwargs: dict[str, object] = {
        "name": "orders",
        "backend": Backend.POSTGRES,
        "table": "orders",
        "alias": "o",
        "measures": [
            Measure(
                name="revenue",
                sql="{o}.amount",
                agg="sum",
                unit="currency",
            ),
            Measure(name="orders", sql="*", agg="count", unit="count"),
            Measure(
                name="conversion_rate",
                sql="{o}.x",
                agg="avg",
                unit="pct",
            ),
        ],
        "dimensions": [
            Dimension(name="region", sql="{o}.region", type="string"),
            Dimension(name="status", sql="{o}.status", type="string"),
        ],
        "time_dimensions": [
            TimeDimension(name="created_at", sql="{o}.created_at"),
        ],
    }
    if default_chart_type is not None:
        kwargs["default_chart_type"] = default_chart_type
    return Cube(**kwargs)  # type: ignore[arg-type]


def _customers(default_chart_type: object | None = None) -> Cube:
    kwargs: dict[str, object] = {
        "name": "customers",
        "backend": Backend.POSTGRES,
        "table": "customers",
        "alias": "c",
        "measures": [Measure(name="count", sql="*", agg="count", unit="count")],
        "dimensions": [Dimension(name="region", sql="{c}.region", type="string")],
    }
    if default_chart_type is not None:
        kwargs["default_chart_type"] = default_chart_type
    return Cube(**kwargs)  # type: ignore[arg-type]


def _catalog(*cubes: Cube) -> dict[str, Cube]:
    return {c.name: c for c in cubes}


# ---------------------------------------------------------------------------
# Branch: cube default_chart_type override
# ---------------------------------------------------------------------------


def test_single_override_wins_regardless_of_shape() -> None:
    cube = _orders(default_chart_type="data_table")
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue"],
        n_rows=2,  # would otherwise be a pie chart
        catalog=_catalog(cube),
    )
    assert decision.chart_type == "data_table"
    assert "default_chart_type" in decision.reason


def test_conflicting_overrides_fall_through_to_normal_logic() -> None:
    """Two cubes touched with *different* default_chart_type values
    cancel each other out so the normal decision logic runs."""
    orders = _orders(default_chart_type="bar_chart")
    customers = _customers(default_chart_type="pie_chart")
    # touch both cubes by including a region dimension and a count
    # measure from each (via a join in the compile path) — for the
    # viz decision we can just pass both into the catalog and reference
    # one cube's fields; the resolver only walks the query, so we
    # need a query that touches both cubes.
    orders_join = orders.model_copy(
        update={
            "joins": [
                {"to": "customers", "relationship": "many_to_one", "on": "..."},
            ]
        }
    )
    decision = decide_visualization(
        query=SemanticQuery(
            measures=["orders.revenue"],
            dimensions=["customers.region"],
        ),
        columns=["region", "revenue"],
        n_rows=2,
        catalog=_catalog(orders_join, customers),
    )
    # Two distinct overrides → fall through to the n_rows/n_dims logic;
    # 1 dim + 1 measure + n_rows<=PIE_MAX_SLICES → pie chart.
    assert decision.chart_type == "pie_chart"


def test_matching_overrides_count_as_one() -> None:
    """Both cubes share the *same* override → still wins."""
    orders = _orders(default_chart_type="data_table")
    customers = _customers(default_chart_type="data_table")
    orders_join = orders.model_copy(
        update={
            "joins": [
                {"to": "customers", "relationship": "many_to_one", "on": "..."},
            ]
        }
    )
    decision = decide_visualization(
        query=SemanticQuery(
            measures=["orders.revenue"],
            dimensions=["customers.region"],
        ),
        columns=["region", "revenue"],
        n_rows=2,
        catalog=_catalog(orders_join, customers),
    )
    assert decision.chart_type == "data_table"


# ---------------------------------------------------------------------------
# Branch: ungrouped → data_table
# ---------------------------------------------------------------------------


def test_ungrouped_always_data_table() -> None:
    decision = decide_visualization(
        query=SemanticQuery(dimensions=["orders.region"], ungrouped=True, limit=10),
        columns=["region"],
        n_rows=5,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type == "data_table"
    assert "ungrouped" in decision.reason


# ---------------------------------------------------------------------------
# Branch: text_only — single measure, no dimensions
# ---------------------------------------------------------------------------


def test_single_measure_no_dim_returns_text_only() -> None:
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"]),
        columns=["revenue"],
        n_rows=1,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type == "text_only"


# ---------------------------------------------------------------------------
# Branch: line_chart — time series with granularity
# ---------------------------------------------------------------------------


def test_time_breakdown_returns_line_chart() -> None:
    decision = decide_visualization(
        query=SemanticQuery(
            measures=["orders.revenue"],
            time_dimension=TimeWindow(
                dimension="orders.created_at",
                granularity="day",
                range=("2026-01-01", "2026-02-01"),
            ),
        ),
        columns=["created_at_day", "revenue"],
        n_rows=31,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type == "line_chart"


def test_time_without_granularity_is_not_line_chart() -> None:
    """Time dimension WITHOUT granularity is just a filter — falls
    through to bar / pie / data-table logic."""
    decision = decide_visualization(
        query=SemanticQuery(
            measures=["orders.revenue"],
            dimensions=["orders.region"],
            time_dimension=TimeWindow(
                dimension="orders.created_at",
                range=("2026-01-01", "2026-02-01"),
            ),
        ),
        columns=["region", "revenue"],
        n_rows=2,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type != "line_chart"


# ---------------------------------------------------------------------------
# Branch: pie_chart — 1 dim, 1 measure, n_rows <= PIE_MAX_SLICES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_rows", [1, PIE_MAX_SLICES])
def test_pie_chart_at_and_below_boundary(n_rows: int) -> None:
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue"],
        n_rows=n_rows,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type == "pie_chart"


def test_pie_chart_off_by_one_falls_to_bar() -> None:
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue"],
        n_rows=PIE_MAX_SLICES + 1,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type == "bar_chart"


def test_pie_chart_requires_exactly_one_measure() -> None:
    """2 measures + 1 dim → bar chart (or data_table at large n)."""
    decision = decide_visualization(
        query=SemanticQuery(
            measures=["orders.revenue", "orders.orders"],
            dimensions=["orders.region"],
        ),
        columns=["region", "revenue", "orders"],
        n_rows=3,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type == "bar_chart"


# ---------------------------------------------------------------------------
# Branch: bar_chart — 1 dim, n_rows <= BAR_MAX_BARS
# ---------------------------------------------------------------------------


def test_bar_chart_at_boundary() -> None:
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue"],
        n_rows=BAR_MAX_BARS,
        catalog=_catalog(_orders()),
    )
    # n_rows == BAR_MAX_BARS and > PIE_MAX_SLICES → bar chart.
    assert decision.chart_type == "bar_chart"


def test_bar_chart_off_by_one_falls_to_data_table() -> None:
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue"],
        n_rows=BAR_MAX_BARS + 1,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type == "data_table"


# ---------------------------------------------------------------------------
# Branch: data_table — multi-dim
# ---------------------------------------------------------------------------


def test_multi_dim_returns_data_table() -> None:
    decision = decide_visualization(
        query=SemanticQuery(
            measures=["orders.revenue"],
            dimensions=["orders.region", "orders.status"],
        ),
        columns=["region", "status", "revenue"],
        n_rows=4,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type == "data_table"


# ---------------------------------------------------------------------------
# Output shape — VizColumn / axis labels / title / format inference
# ---------------------------------------------------------------------------


def test_columns_are_populated_with_per_column_metadata() -> None:
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue"],
        n_rows=3,
        catalog=_catalog(_orders()),
    )
    assert [c.name for c in decision.columns] == ["region", "revenue"]
    revenue_col = next(c for c in decision.columns if c.name == "revenue")
    assert revenue_col.is_measure is True
    # unit="currency" is not in the inference table (only pct/count/duration);
    # default falls through to "raw". Callers wanting "currency" set format=
    # explicitly on the Measure.
    assert revenue_col.format == "raw"
    region_col = next(c for c in decision.columns if c.name == "region")
    assert region_col.is_measure is False
    assert region_col.is_time is False


def test_time_dimension_column_marked_is_time() -> None:
    decision = decide_visualization(
        query=SemanticQuery(
            measures=["orders.revenue"],
            time_dimension=TimeWindow(
                dimension="orders.created_at",
                granularity="day",
                range=("2026-01-01", "2026-02-01"),
            ),
        ),
        columns=["created_at_day", "revenue"],
        n_rows=10,
        catalog=_catalog(_orders()),
    )
    ts_col = next(c for c in decision.columns if c.name == "created_at_day")
    assert ts_col.is_time is True


def test_unknown_columns_get_default_viz_column() -> None:
    """A column in ``columns`` that doesn't correspond to a referenced
    measure / dimension still surfaces with sensible defaults."""
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue", "mystery_column"],
        n_rows=3,
        catalog=_catalog(_orders()),
    )
    mystery = next(c for c in decision.columns if c.name == "mystery_column")
    assert mystery.format == "raw"
    assert mystery.display_name == "Mystery Column"  # _humanize converts


# ---------------------------------------------------------------------------
# Format inference per unit
# ---------------------------------------------------------------------------


def test_unit_count_becomes_integer_format() -> None:
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.orders"], dimensions=["orders.region"]),
        columns=["region", "orders"],
        n_rows=3,
        catalog=_catalog(_orders()),
    )
    orders_col = next(c for c in decision.columns if c.name == "orders")
    assert orders_col.format == "integer"


def test_unit_pct_becomes_percent_format() -> None:
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.conversion_rate"], dimensions=["orders.region"]),
        columns=["region", "conversion_rate"],
        n_rows=3,
        catalog=_catalog(_orders()),
    )
    col = next(c for c in decision.columns if c.name == "conversion_rate")
    assert col.format == "percent"


def test_explicit_format_overrides_unit_inference() -> None:
    """``Measure.format`` if explicitly set wins over unit-based guess."""
    cube = _orders().model_copy(
        update={
            "measures": [
                Measure(
                    name="revenue",
                    sql="{o}.x",
                    agg="sum",
                    unit="count",  # would infer "integer"
                    format="percent",  # but explicit wins
                ),
            ],
        }
    )
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue"],
        n_rows=3,
        catalog={"orders": cube.model_copy(update={"dimensions": _orders().dimensions})},
    )
    col = next(c for c in decision.columns if c.name == "revenue")
    assert col.format == "percent"


# ---------------------------------------------------------------------------
# Title and axis labels
# ---------------------------------------------------------------------------


def test_title_combines_measure_and_dimension_labels() -> None:
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue"],
        n_rows=3,
        catalog=_catalog(_orders()),
    )
    assert "Revenue" in decision.title
    assert "Region" in decision.title


def test_bar_chart_axis_labels_filled() -> None:
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue"],
        n_rows=15,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type == "bar_chart"
    assert decision.x_axis == "Region"
    assert decision.y_axes == ["Revenue"]


def test_pie_chart_axes_labels_single_value() -> None:
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue"],
        n_rows=3,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type == "pie_chart"
    assert decision.x_axis == "Region"
    assert decision.y_axes == ["Revenue"]


def test_data_table_has_no_axes() -> None:
    decision = decide_visualization(
        query=SemanticQuery(
            measures=["orders.revenue"],
            dimensions=["orders.region", "orders.status"],
        ),
        columns=["region", "status", "revenue"],
        n_rows=4,
        catalog=_catalog(_orders()),
    )
    assert decision.chart_type == "data_table"
    assert decision.x_axis is None
    assert decision.y_axes == []


# ---------------------------------------------------------------------------
# Display name overrides _humanize
# ---------------------------------------------------------------------------


def test_explicit_display_name_overrides_humanize() -> None:
    cube = _orders().model_copy(
        update={
            "measures": [
                Measure(
                    name="revenue",
                    sql="{o}.x",
                    agg="sum",
                    unit="currency",
                    display_name="Net Revenue (USD)",
                ),
            ],
            "dimensions": [
                Dimension(
                    name="region",
                    sql="{o}.region",
                    type="string",
                    display_name="Sales Region",
                ),
            ],
        }
    )
    decision = decide_visualization(
        query=SemanticQuery(measures=["orders.revenue"], dimensions=["orders.region"]),
        columns=["region", "revenue"],
        n_rows=3,
        catalog={"orders": cube},
    )
    revenue_col = next(c for c in decision.columns if c.name == "revenue")
    region_col = next(c for c in decision.columns if c.name == "region")
    assert revenue_col.display_name == "Net Revenue (USD)"
    assert region_col.display_name == "Sales Region"


# ---------------------------------------------------------------------------
# VizColumn dataclass shape
# ---------------------------------------------------------------------------


def test_viz_column_can_be_constructed_directly() -> None:
    col = VizColumn(name="x", display_name="X", format="raw", is_measure=False, is_time=False)
    assert col.name == "x"
    assert col.format == "raw"
