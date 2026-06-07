"""Catalogue introspection — two complementary surfaces.

**SQL surface** (``META_CUBES`` and friends): the catalogue itself as
queryable ``Backend.META`` cubes. ``_emit_cube_source`` in ``compile.py``
materialises these as ``VALUES`` literals so a planner can ask
"which measures are seconds-typed?" via an ordinary ``SemanticQuery``.

**Python surface** (``iter_cubes``, ``iter_fields``, ``iter_joins``,
``resolve_field``): walk the catalogue from Python. Every downstream
tool (prompt rendering, MCP exposure, ER diagrams, live-DB validation)
needs to filter META cubes, honour ``expose_in_prompt``, and iterate
fields/joins. These primitives centralise the patterns so consumers
share one definition of "what counts as a real cube" and "what fields
live on this cube."

Both surfaces reflect the same data; pick the one that matches the
caller (SQL planner vs Python tool).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import TYPE_CHECKING

from semql.model import Backend, BaseField, Cube, Dimension, Join, Measure, TimeDimension, View

if TYPE_CHECKING:
    pass

CatalogLike = "Mapping[str, Cube] | Catalog | Iterable[Cube]"
"""Anything we can iterate cubes from. Concrete types: ``Catalog``
(iter yields cubes), ``dict[str, Cube]`` (values yield cubes), or any
``Iterable[Cube]``. Use ``_iter_all_cubes`` to normalise."""


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


# ---------------------------------------------------------------------------
# Python introspection — iterate over a catalogue's shape
# ---------------------------------------------------------------------------


def _iter_all_cubes(catalog: object) -> Iterator[Cube]:
    """Normalise any ``CatalogLike`` to an ``Iterator[Cube]``.

    Accepts the public ``Catalog`` wrapper (iterable of Cube), a
    ``dict[str, Cube]`` (compile_query's canonical shape), or any
    ``Iterable[Cube]``. ``Mapping`` is checked first so a ``dict`` is
    treated by its values, not its keys."""
    if isinstance(catalog, Mapping):
        yield from catalog.values()
        return
    # Catalog and arbitrary Iterable[Cube] both work via plain iteration.
    yield from catalog  # type: ignore[misc]


def iter_cubes(
    catalog: object,
    *,
    include_meta: bool = False,
    only_exposed: bool = False,
) -> Iterator[Cube]:
    """Yield cubes from a catalogue with consistent filtering.

    ``include_meta=False`` (default) skips ``Backend.META`` reflection
    cubes — the right default for any tool that walks real database
    tables (validate-db, ERD, MCP). Set ``True`` to include them when
    building prompt fragments that document reflection.

    ``only_exposed=True`` skips cubes flagged ``expose_in_prompt=False`` —
    the right default for LLM-facing surfaces (prompt rendering, MCP
    auto-tools). Leave ``False`` to include hidden cubes (ERD,
    validate-db).
    """
    for cube in _iter_all_cubes(catalog):
        if not include_meta and cube.backend is Backend.META:
            continue
        if only_exposed and not cube.expose_in_prompt:
            continue
        yield cube


def iter_fields(cube: Cube) -> Iterator[BaseField]:
    """Yield every addressable field on a cube, in declaration order.

    Order: measures, dimensions, time_dimensions, segments — the same
    grouping the catalogue uses in its rendering and validation paths.
    ``isinstance(f, Measure)`` etc. still narrows because ``BaseField``
    is a structural supertype, not a discriminator."""
    yield from cube.measures
    yield from cube.dimensions
    yield from cube.time_dimensions
    yield from cube.segments


def iter_joins(
    catalog: object,
    *,
    include_meta: bool = False,
) -> Iterator[tuple[Cube, Join, Cube]]:
    """Yield ``(source, edge, target)`` triples for every Join in the catalog.

    Targets are looked up by ``Join.to``; a Join whose target is missing
    from the catalog is silently skipped (Catalog construction already
    rejects this case, so missing targets in practice mean the caller
    passed a hand-built dict; staying quiet is the friendliest behaviour).
    """
    cubes = list(iter_cubes(catalog, include_meta=include_meta))
    by_name = {c.name: c for c in cubes}
    for cube in cubes:
        for join in cube.joins:
            target = by_name.get(join.to)
            if target is None:
                continue
            yield cube, join, target


def resolve_field(
    qualified: str,
    catalog: object,
    *,
    views: Mapping[str, View] | None = None,
) -> tuple[Cube, Measure | Dimension | TimeDimension]:
    """Resolve a ``cube.field`` (or ``view.field``) reference.

    Mirrors the compiler's resolution path so tools share one definition
    of what's addressable. When ``views`` is provided and the qualifier
    matches a view name, the returned ``Field`` carries the view's
    *local* name (so SELECT aliases match the planner's reference).

    Raises whatever the resolver raises on unknown identifiers — the
    underlying ``ResolveError`` / ``UnknownIdentifierError`` hierarchy
    from ``semql.errors``.
    """
    # Build the dict shape the underlying resolver wants. Local import
    # keeps the module cycle-free at import time.
    from semql._resolve import resolve_field as _resolve

    by_name: dict[str, Cube] = {c.name: c for c in _iter_all_cubes(catalog)}
    if views and "." in qualified:
        prefix, local = qualified.split(".", 1)
        if prefix in views:
            view = views[prefix]
            if local not in view.fields:
                from semql.errors import ResolveError

                raise ResolveError(
                    f"View {prefix!r} has no field {local!r}. "
                    f"Known fields on this view: {sorted(view.fields)}."
                )
            cube, fld = _resolve(view.fields[local], by_name)
            # Carry the local name so callers building output columns
            # match what the view exposes.
            return cube, fld.model_copy(update={"name": local})
    return _resolve(qualified, by_name)


__all__ = [
    "CATALOG_CUBES",
    "CATALOG_DIMENSIONS",
    "CATALOG_MEASURES",
    "META_CUBES",
    "build_meta_values",
    "iter_cubes",
    "iter_fields",
    "iter_joins",
    "quote_literal",
    "resolve_field",
]
