"""Reflection cubes — expose the catalogue itself as queryable cubes.

The semantic layer's catalogue is just Python data. With three thin
`Backend.META` cubes we can ask "list cubes by backend", "which
measures are seconds-typed", "which dimensions return strings" as
ordinary `SemanticQuery` inputs — same spec, same compiler, same
output shape.

`_emit_cube_source` in `compile.py` dispatches META cubes here;
we materialise the catalogue snapshot as a SQL VALUES literal at
compile time. The result is portable VALUES syntax with no tenant
tables.

Catalogue self-reference: `catalog_cubes` lists itself. The VALUES
materialisation reads the catalogue dict at compile time, so adding
or removing a cube changes future queries without code edits.
"""

from __future__ import annotations

from collections.abc import Iterable

from semql.model import Backend, Cube, Dimension, Measure


def quote_literal(value: str | None) -> str:
    """PG-style single-quoted string literal. Descriptions can contain
    apostrophes so we escape them."""
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def _bool(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def _rows_to_values(rows: Iterable[tuple[str, ...]], columns: list[str]) -> str:
    """Wrap row tuples as a self-aliased SELECT over VALUES."""
    row_list = list(rows)
    if not row_list:
        nulls = ", ".join(["NULL"] * len(columns))
        return f"(SELECT * FROM (VALUES ({nulls})) AS _v({', '.join(columns)}) WHERE FALSE)"
    values_sql = ", ".join("(" + ", ".join(row) + ")" for row in row_list)
    return f"(SELECT * FROM (VALUES {values_sql}) AS _v({', '.join(columns)}))"


def build_meta_values(cube_name: str, catalog: dict[str, Cube]) -> str:
    """Materialise the catalogue snapshot as a VALUES subquery for the
    given META cube. Called from `compile._emit_cube_source`."""
    if cube_name == "catalog_cubes":
        rows = [
            (
                quote_literal(c.name),
                quote_literal(c.backend.value),
                _bool(c.expose_in_prompt),
                quote_literal(c.description),
                quote_literal(c.alias),
            )
            for c in catalog.values()
        ]
        return _rows_to_values(rows, ["name", "backend", "exposed", "description", "alias"])

    if cube_name == "catalog_measures":
        rows = [
            (
                quote_literal(c.name),
                quote_literal(m.name),
                quote_literal(m.agg),
                quote_literal(m.unit),
                quote_literal(m.description),
            )
            for c in catalog.values()
            for m in c.measures
        ]
        return _rows_to_values(rows, ["cube", "name", "agg", "unit", "description"])

    if cube_name == "catalog_dimensions":
        dim_rows: list[tuple[str, ...]] = []
        for c in catalog.values():
            for d in c.dimensions:
                dim_rows.append(
                    (
                        quote_literal(c.name),
                        quote_literal(d.name),
                        quote_literal(d.type),
                        quote_literal(d.description),
                        _bool(False),
                    )
                )
            for td in c.time_dimensions:
                dim_rows.append(
                    (
                        quote_literal(c.name),
                        quote_literal(td.name),
                        quote_literal(td.type),
                        quote_literal(td.description),
                        _bool(True),
                    )
                )
        return _rows_to_values(dim_rows, ["cube", "name", "type", "description", "is_time"])

    raise KeyError(f"No META builder for cube {cube_name!r}.")


# ---------------------------------------------------------------------------
# META cube definitions
# ---------------------------------------------------------------------------

CATALOG_CUBES = Cube(
    name="catalog_cubes",
    backend=Backend.META,
    table="catalog_cubes",
    alias="cc",
    expose_in_prompt=False,
    default_chart_type="data_table",
    measures=[Measure(name="count", sql="*", agg="count", unit="count")],
    dimensions=[
        Dimension(name="name", sql="{cc}.name", type="string"),
        Dimension(name="backend", sql="{cc}.backend", type="string"),
        Dimension(name="exposed", sql="{cc}.exposed", type="bool"),
        Dimension(name="description", sql="{cc}.description", type="string"),
        Dimension(name="alias", sql="{cc}.alias", type="string"),
    ],
    description="One row per cube in the catalogue.",
)

CATALOG_MEASURES = Cube(
    name="catalog_measures",
    backend=Backend.META,
    table="catalog_measures",
    alias="cm",
    expose_in_prompt=False,
    default_chart_type="data_table",
    measures=[Measure(name="count", sql="*", agg="count", unit="count")],
    dimensions=[
        Dimension(name="cube", sql="{cm}.cube", type="string"),
        Dimension(name="name", sql="{cm}.name", type="string"),
        Dimension(name="agg", sql="{cm}.agg", type="string"),
        Dimension(name="unit", sql="{cm}.unit", type="string"),
        Dimension(name="description", sql="{cm}.description", type="string"),
    ],
    description="One row per (cube, measure). Use to find measures by unit / agg.",
)

CATALOG_DIMENSIONS = Cube(
    name="catalog_dimensions",
    backend=Backend.META,
    table="catalog_dimensions",
    alias="cd",
    expose_in_prompt=False,
    default_chart_type="data_table",
    measures=[Measure(name="count", sql="*", agg="count", unit="count")],
    dimensions=[
        Dimension(name="cube", sql="{cd}.cube", type="string"),
        Dimension(name="name", sql="{cd}.name", type="string"),
        Dimension(name="type", sql="{cd}.type", type="string"),
        Dimension(name="description", sql="{cd}.description", type="string"),
        Dimension(name="is_time", sql="{cd}.is_time", type="bool"),
    ],
    description="One row per (cube, dimension or time_dimension). is_time distinguishes them.",
)

META_CUBES: list[Cube] = [CATALOG_CUBES, CATALOG_MEASURES, CATALOG_DIMENSIONS]

__all__ = [
    "CATALOG_CUBES",
    "CATALOG_MEASURES",
    "CATALOG_DIMENSIONS",
    "META_CUBES",
    "build_meta_values",
    "quote_literal",
]
