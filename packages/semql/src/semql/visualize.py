"""Deterministic visualization decision from a `SemanticQuery`.

Given a compiled query result, we know:
  - Whether a time dimension was requested (and at what granularity).
  - Each measure's unit and explicit display name.
  - Each dimension's type and explicit display name.
  - How many rows the query produced (passed in as `n_rows`).
  - Whether the cube has a `default_chart_type` override.

That's enough to pick chart type, axes, formats, and labels in code.
The function returns a `VizDecision`; callers can serialise it as a
hint for a presenter LLM or apply it directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from semql.model import (
    ChartTypeLiteral,
    Cube,
    Dimension,
    FormatLiteral,
    Measure,
    TimeDimension,
)
from semql.spec import SemanticQuery

PIE_MAX_SLICES = 10
BAR_MAX_BARS = 30


@dataclass
class VizColumn:
    """Per-output-column presentation metadata. Order matches `Compiled.columns`."""

    name: str
    display_name: str
    format: FormatLiteral
    is_measure: bool
    is_time: bool


@dataclass
class VizDecision:
    chart_type: ChartTypeLiteral | Literal["text_only"]
    title: str
    x_axis: str | None
    y_axes: list[str]
    columns: list[VizColumn]
    reason: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _humanize(name: str) -> str:
    return name.replace("_", " ").title()


def _infer_format(measure: Measure) -> FormatLiteral:
    if measure.format is not None:
        return measure.format
    unit = (measure.unit or "").lower()
    if unit in ("pct", "percent"):
        return "percent"
    if unit == "count":
        return "integer"
    if unit in ("seconds", "minutes", "hours", "duration"):
        return "duration"
    return "raw"


def _label_for_measure(m: Measure) -> str:
    return m.display_name or _humanize(m.name)


def _label_for_dimension(d: Dimension | TimeDimension) -> str:
    return d.display_name or _humanize(d.name)


# ---------------------------------------------------------------------------
# Chart-type decision — pure on the spec
# ---------------------------------------------------------------------------


def _pick_chart_type(
    query: SemanticQuery,
    touched_cubes: list[Cube],
    n_rows: int,
) -> tuple[ChartTypeLiteral | Literal["text_only"], str]:
    overrides: set[ChartTypeLiteral] = {
        c.default_chart_type for c in touched_cubes if c.default_chart_type is not None
    }
    if len(overrides) == 1:
        chart = next(iter(overrides))
        return chart, f"cube default_chart_type={chart}"

    if query.ungrouped:
        return "data_table", "ungrouped row listing"

    n_measures = len(query.measures)
    has_time_breakdown = (
        query.time_dimension is not None and query.time_dimension.granularity is not None
    )
    n_dims = len(query.dimensions) + (1 if has_time_breakdown else 0)

    if n_measures >= 1 and n_dims == 0:
        return "text_only", "single-value answer"

    if has_time_breakdown:
        return "line_chart", "time series with granularity"

    if n_dims == 1 and n_measures == 1 and n_rows <= PIE_MAX_SLICES:
        return "pie_chart", f"1 dim, 1 measure, n_rows={n_rows} <= {PIE_MAX_SLICES}"

    if n_dims == 1 and n_rows <= BAR_MAX_BARS:
        return "bar_chart", f"1 dim, n_rows={n_rows} <= {BAR_MAX_BARS}"

    return "data_table", f"multi-dim or n_rows={n_rows} too large for a chart"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def decide_visualization(
    query: SemanticQuery,
    columns: list[str],
    n_rows: int,
    *,
    catalog: dict[str, Cube],
) -> VizDecision:
    """Return chart_type + axis labels + per-column formats for a query.

    `columns` — the `Compiled.columns` list from the compiler.
    `n_rows` — actual row count; pass 0 for dry-run / explain paths.
    `catalog` — cube dict (from `Catalog.as_dict()`).
    """
    from semql.introspect import resolve_query

    resolved = resolve_query(query, catalog)
    measure_meta = resolved.measures
    dim_meta = resolved.dimensions
    time_meta = resolved.time_dimension
    touched = resolved.touched_cubes

    chart_type, reason = _pick_chart_type(query, touched, n_rows)

    col_meta: dict[str, VizColumn] = {}
    for _, d in dim_meta:
        col_meta[d.name] = VizColumn(
            name=d.name,
            display_name=_label_for_dimension(d),
            format="raw",
            is_measure=False,
            is_time=False,
        )
    if time_meta is not None:
        _, td = time_meta
        gran = query.time_dimension.granularity if query.time_dimension else None
        col_name = f"{td.name}_{gran}" if gran else td.name
        col_meta[col_name] = VizColumn(
            name=col_name,
            display_name=_label_for_dimension(td),
            format="raw",
            is_measure=False,
            is_time=True,
        )
    for _, m in measure_meta:
        col_meta[m.name] = VizColumn(
            name=m.name,
            display_name=_label_for_measure(m),
            format=_infer_format(m),
            is_measure=True,
            is_time=False,
        )

    ordered: list[VizColumn] = []
    for col in columns:
        if col in col_meta:
            ordered.append(col_meta[col])
        else:
            ordered.append(
                VizColumn(
                    name=col,
                    display_name=_humanize(col),
                    format="raw",
                    is_measure=False,
                    is_time=False,
                )
            )

    x_axis: str | None = None
    y_axes: list[str] = []
    if chart_type in ("bar_chart", "line_chart"):
        non_measures = [c for c in ordered if not c.is_measure]
        measures_out = [c for c in ordered if c.is_measure]
        if non_measures:
            x_axis = non_measures[0].display_name
        y_axes = [c.display_name for c in measures_out]
    elif chart_type == "pie_chart":
        non_measures = [c for c in ordered if not c.is_measure]
        measures_out = [c for c in ordered if c.is_measure]
        x_axis = non_measures[0].display_name if non_measures else None
        y_axes = [measures_out[0].display_name] if measures_out else []

    title_bits: list[str] = []
    if measure_meta:
        title_bits.append(_label_for_measure(measure_meta[0][1]))
    if dim_meta:
        title_bits.append("by " + _label_for_dimension(dim_meta[0][1]))
    elif time_meta is not None:
        title_bits.append("over " + _label_for_dimension(time_meta[1]))
    title = " ".join(title_bits) if title_bits else "Result"

    return VizDecision(
        chart_type=chart_type,
        title=title,
        x_axis=x_axis,
        y_axes=y_axes,
        columns=ordered,
        reason=reason,
    )


__all__ = [
    "BAR_MAX_BARS",
    "PIE_MAX_SLICES",
    "VizColumn",
    "VizDecision",
    "decide_visualization",
]
