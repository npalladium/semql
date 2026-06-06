"""Type definitions for the semantic catalogue.

A `Cube` declares one logical table: where its rows live (`backend`,
`table`), the always-on predicate that defines membership
(`base_predicate`), the measures/dimensions/time-dimensions exposed,
and the join edges to other cubes.

`expose_in_prompt` controls whether `render_catalogue_block` includes
the cube in the system-prompt fragment shown to the planner. The
catalogue is intentionally wider than the prompt — every cube the
compiler accepts doesn't need to be in the planner's vocabulary. Cubes
flagged `False` are reachable via joins from exposed cubes and still
compile cleanly when the planner names them; they just don't appear in
the catalogue rendering.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Backend(StrEnum):
    POSTGRES = "postgres"
    CLICKHOUSE = "clickhouse"
    DUCKDB = "duckdb"
    BIGQUERY = "bigquery"
    SNOWFLAKE = "snowflake"
    META = "meta"  # reflection over the catalogue itself; see introspect.py


AggLiteral = Literal["sum", "count", "count_distinct", "avg", "min", "max"]
DimTypeLiteral = Literal["string", "number", "time", "bool", "uuid"]
GranularityLiteral = Literal["hour", "day", "week", "month"]
FormatLiteral = Literal["raw", "integer", "percent", "currency", "duration"]
ChartTypeLiteral = Literal["pie_chart", "bar_chart", "line_chart", "data_table"]


class Measure(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    sql: str
    agg: AggLiteral
    unit: str | None = None
    description: str = ""
    display_name: str | None = None
    format: FormatLiteral | None = None


class Dimension(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    sql: str
    type: DimTypeLiteral
    description: str = ""
    display_name: str | None = None


class TimeDimension(BaseModel):
    """Time dimensions are separated so the compiler can apply
    `granularity` truncation when a query asks for it (hourly/daily/etc.
    rollups). Otherwise they're just dimensions of type `time`."""

    model_config = ConfigDict(frozen=True)
    name: str
    sql: str
    type: Literal["time"] = "time"
    granularities: tuple[GranularityLiteral, ...] = ("hour", "day", "week", "month")
    description: str = ""
    display_name: str | None = None


class Join(BaseModel):
    """A directed edge from one cube to another.

    `on` is a SQL fragment using `{alias}` placeholders that the
    compiler resolves to actual table aliases at compile time."""

    model_config = ConfigDict(frozen=True)
    to: str
    relationship: Literal["one_to_one", "one_to_many", "many_to_one"]
    on: str


class Cube(BaseModel):
    name: str
    backend: Backend
    table: str
    alias: str
    base_predicate: str | None = None
    measures: list[Measure] = []
    dimensions: list[Dimension] = []
    time_dimensions: list[TimeDimension] = []
    joins: list[Join] = []
    # Dimensions on this cube that MUST appear in a query's `filters`
    # (any operator, any value) before the compiler will accept the
    # query.
    required_filters: list[str] = []
    expose_in_prompt: bool = True
    description: str = ""
    display_name: str | None = None
    default_chart_type: ChartTypeLiteral | None = None

    def field_names(self) -> set[str]:
        names: set[str] = set()
        names.update(m.name for m in self.measures)
        names.update(d.name for d in self.dimensions)
        names.update(td.name for td in self.time_dimensions)
        return names


__all__ = [
    "AggLiteral",
    "Backend",
    "ChartTypeLiteral",
    "Cube",
    "Dimension",
    "DimTypeLiteral",
    "FormatLiteral",
    "GranularityLiteral",
    "Join",
    "Measure",
    "TimeDimension",
]
