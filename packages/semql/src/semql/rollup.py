"""Rollup routing — match a SemanticQuery against a cube's pre-aggregated
rollup tables and rewrite the cube to read from the rollup when one fits.

Two functions:

- :func:`pick_rollup` decides which rollup (if any) covers the query.
  Phase-1 matching is conservative — exact-grain (query time grain ==
  rollup grain), all referenced dims / measures stored, every filter
  touches only stored columns, no joins / segments / compare windows.
  When multiple rollups match, the one with the fewest stored dims
  wins (smaller table = faster read).

- :func:`apply_rollup` returns a *synthetic catalog* — the original
  ``cube_by_name`` dict with the matched cube replaced by a version
  whose ``table`` points at the rollup's ``physical_table`` and whose
  measure / dimension / time-dimension SQL fragments address the
  rollup's bucketed columns. Downstream compilation against this
  synthetic catalog produces SQL against the rollup table without
  any awareness that re-routing happened.

The integration point in ``compile_query`` calls these two functions
inside ``_CompileEnv.__init__`` so every downstream stage sees the
rewritten catalog uniformly. The applied rollup name is surfaced on
``CompiledQuery.applied_rollup`` for observability.
"""

from __future__ import annotations

from collections.abc import Callable

from semql.model import (
    Cube,
    Dimension,
    Measure,
    Rollup,
    TimeDimension,
)
from semql.spec import BoolExpr, Filter, SemanticQuery


def _referenced_cubes(query: SemanticQuery) -> set[str]:
    """Set of cube names referenced by the query.

    Walks every ``cube.field`` ref and segment ref. Returns names; the
    caller decides what to do when more than one cube is touched. Note
    this does NOT walk join paths — those are catalog-derived and
    aren't on the query itself."""
    cubes: set[str] = set()

    def _add(ref: str) -> None:
        if "." in ref:
            cubes.add(ref.split(".", 1)[0])

    for m in query.measures:
        _add(m)
    for d in query.dimensions:
        _add(d)
    if query.time_dimension is not None:
        _add(query.time_dimension.dimension)
    for f in query.filters:
        _add(f.dimension)
    for f in query.having:
        # ``having`` references can be bare measure names (resolved later
        # against the derived measures' aliases) — only count qualified ones.
        _add(f.dimension)
    for s in query.segments:
        _add(s)
    for order_key, _ in query.order:
        _add(order_key)
    if query.where is not None:
        _walk_bool_expr(query.where, cubes)
    return cubes


def _walk_bool_expr(node: BoolExpr | Filter, cubes: set[str]) -> None:
    if isinstance(node, Filter):
        if "." in node.dimension:
            cubes.add(node.dimension.split(".", 1)[0])
        return
    for child in node.children:
        _walk_bool_expr(child, cubes)


def _filter_dims(query: SemanticQuery) -> set[str]:
    """Set of dim local-names touched by filters (filters + where tree).

    Each ref is ``cube.dim`` — return only the local name. The caller is
    expected to have already established the query is single-cube, so
    these can be matched directly against a rollup's ``dimensions``."""
    dims: set[str] = set()

    def _add(ref: str) -> None:
        dims.add(ref.split(".", 1)[1] if "." in ref else ref)

    for f in query.filters:
        _add(f.dimension)
    if query.where is not None:
        _walk_where(query.where, _add)
    return dims


def _walk_where(
    node: BoolExpr | Filter,
    callback: Callable[[str], None],
) -> None:
    if isinstance(node, Filter):
        callback(node.dimension)
        return
    for child in node.children:
        _walk_where(child, callback)


def _matches(
    query: SemanticQuery,
    rollup: Rollup,
) -> bool:
    """True iff the rollup can answer the query at its stored grain.

    Conditions (Phase 1, conservative):

    1. Every measure ref ``cube.M`` → ``M`` in ``rollup.measures``.
    2. Every dimension ref ``cube.D`` → ``D`` in ``rollup.dimensions``.
    3. If the query has a ``time_dimension``: ``time_dim.dimension`` →
       local name must equal ``rollup.time_dimension`` AND
       ``time_dim.granularity`` must equal ``rollup.granularity``.
       If the query has no ``time_dimension``, any rollup time-grain
       is OK (the rollup just over-aggregates; SUM-of-SUMs is correct).
    4. Every filter dim is in ``rollup.dimensions`` (or is the rollup's
       stored time_dimension when its local name matches).
    5. Every having ref names a measure in ``rollup.measures`` (or one
       of the derived_measures, whose operands are all in the rollup).
    6. No segments — segment SQL is templated against the base table.
    7. No left_joins (Phase 1 routes only single-cube queries).
    8. No CompareWindow — compare-mode CTEs need row-level time access.
    9. No derived_measures whose operands aren't all stored.
    """
    rollup_measures = set(rollup.measures)
    rollup_dims = set(rollup.dimensions)

    if query.compare is not None:
        return False
    if query.segments:
        return False
    if query.left_joins:
        return False

    for m_ref in query.measures:
        local = m_ref.split(".", 1)[1] if "." in m_ref else m_ref
        if local not in rollup_measures:
            return False

    for d_ref in query.dimensions:
        local = d_ref.split(".", 1)[1] if "." in d_ref else d_ref
        if local not in rollup_dims:
            return False

    if query.time_dimension is not None:
        td_local = query.time_dimension.dimension.split(".", 1)[1]
        if rollup.time_dimension != td_local:
            return False
        if rollup.granularity != query.time_dimension.granularity:
            return False

    filter_dims = _filter_dims(query)
    for fdim in filter_dims:
        if fdim in rollup_dims:
            continue
        # Time dim filter is OK iff the rollup stores the same time
        # dim — the bucket column doubles as the filter target.
        if rollup.time_dimension is not None and fdim == rollup.time_dimension:
            continue
        return False

    for having_filter in query.having:
        local = (
            having_filter.dimension.split(".", 1)[1]
            if "." in having_filter.dimension
            else having_filter.dimension
        )
        # Bare names (derived measure aliases) — accept iff the named
        # derived measure's operands all map to rollup measures.
        if "." not in having_filter.dimension:
            inline = next(
                (im for im in query.derived_measures if im.name == local),
                None,
            )
            if inline is None:
                return False
            if not all(op.split(".", 1)[1] in rollup_measures for op in inline.operands):
                return False
        else:
            if local not in rollup_measures:
                return False

    for inline in query.derived_measures:
        if not all(op.split(".", 1)[1] in rollup_measures for op in inline.operands):
            return False

    for order_key, _ in query.order:
        local = order_key.split(".", 1)[1] if "." in order_key else order_key
        if local in rollup_measures or local in rollup_dims:
            continue
        if rollup.time_dimension is not None and local == rollup.time_dimension:
            continue
        if any(im.name == local for im in query.derived_measures):
            continue
        return False

    return True


def pick_rollup(
    query: SemanticQuery,
    catalog: dict[str, Cube],
) -> tuple[Cube, Rollup] | None:
    """Pick the smallest matching rollup for the query, or None.

    Phase 1 routes only single-cube queries. If the query touches more
    than one cube (via joins / cross-cube refs), the function returns
    None and compilation falls through to the base-table path.

    Returns ``(cube, rollup)`` — the caller passes both to
    :func:`apply_rollup` to build the synthetic catalog.
    """
    cubes = _referenced_cubes(query)
    if len(cubes) != 1:
        return None
    (cube_name,) = cubes
    cube = catalog.get(cube_name)
    if cube is None or not cube.rollups:
        return None

    matching = [r for r in cube.rollups if _matches(query, r)]
    if not matching:
        return None

    # Smallest table = fewest stored dimensions. Stable secondary by
    # name so the choice is deterministic when two rollups tie.
    matching.sort(key=lambda r: (len(r.dimensions), r.name))
    return cube, matching[0]


def apply_rollup(
    catalog: dict[str, Cube],
    cube: Cube,
    rollup: Rollup,
) -> dict[str, Cube]:
    """Return a new catalog map with ``cube`` rewritten to read the rollup.

    The substitution preserves the cube's name and alias — only the
    ``table`` source and field SQLs change. Downstream compilation
    sees a Cube with:

    - ``table`` = rollup.physical_table
    - Each Measure listed in ``rollup.measures`` has its ``sql``
      rewritten to ``{alias}.<measure_name>`` (rollup column is named
      after the measure).
    - Each Dimension listed in ``rollup.dimensions`` has its ``sql``
      rewritten to ``{alias}.<dim_name>``.
    - The TimeDimension named in ``rollup.time_dimension`` has its
      ``sql`` rewritten to ``{alias}.<time_dim_name>_<granularity>``
      (the bucketed column). DATE_TRUNC against an already-bucketed
      value is a no-op so the existing compile path still works.

    Fields not in the rollup are dropped — the routing layer has
    already verified the query only references rollup-stored fields.
    """
    alias = cube.alias
    new_measures: list[Measure] = []
    for m in cube.measures:
        if m.name in rollup.measures:
            new_measures.append(m.model_copy(update={"sql": f"{{{alias}}}.{m.name}"}))

    new_dims: list[Dimension] = []
    for d in cube.dimensions:
        if d.name in rollup.dimensions:
            new_dims.append(d.model_copy(update={"sql": f"{{{alias}}}.{d.name}"}))

    new_time_dims: list[TimeDimension] = []
    if rollup.time_dimension is not None and rollup.granularity is not None:
        bucket_col = f"{rollup.time_dimension}_{rollup.granularity}"
        for td in cube.time_dimensions:
            if td.name == rollup.time_dimension:
                new_time_dims.append(
                    td.model_copy(
                        update={
                            "sql": f"{{{alias}}}.{bucket_col}",
                            # Restrict allowed grains to the stored one so
                            # the compiler refuses queries asking for a
                            # different grain against the rollup. (Phase 1
                            # only routes exact-grain anyway, but this is
                            # a defense-in-depth check.)
                            "granularities": (rollup.granularity,),
                        }
                    )
                )

    new_cube = cube.model_copy(
        update={
            "table": rollup.physical_table,
            "source": None,
            "measures": new_measures,
            "dimensions": new_dims,
            "time_dimensions": new_time_dims,
            # No joins on a rollup cube — Phase 1 doesn't route joined queries.
            "joins": [],
            "segments": [],
            "rollups": [],
        }
    )
    out = dict(catalog)
    out[cube.name] = new_cube
    return out


__all__ = ["apply_rollup", "pick_rollup"]
