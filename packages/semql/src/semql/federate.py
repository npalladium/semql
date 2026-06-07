"""Cross-source compilation: emit per-backend fragments + a DuckDB
merge plan when a query's touched cubes span backends.

The sans-io counterpart of :func:`semql.compile.compile_query` for
federated queries. Each fragment is a normal :class:`Compiled` for its
backend; the merge SQL is always DuckDB dialect — DuckDB is the lingua
franca of the federation layer (both for our in-process executor and
for sans-io callers who want to materialise results into DuckDB-
compatible tooling).

v1 restrictions, refused with :class:`FederationError`:

- Measures must live on a single "primary" backend partition. Dim
  cubes on other backends contribute lookup attributes only.
- Bridge joins between partitions must be equality on a single column
  pair, with both sides declared as ``Dimension``\\s on their cubes
  (the federation layer cannot project columns the catalogue hasn't
  named).
- ``q.where`` (the boolean predicate tree) and ``q.compare`` are not
  supported in cross-source v1.
- Aggregations: ``sum`` / ``count`` distribute (sum-of-sums at merge);
  ``avg`` is decomposed into ``(sum, count)`` in the primary fragment
  and recomposed at the merge step. ``count_distinct`` / ``min`` /
  ``max`` / ``ratio`` need raw rows; refused in sans-io. The in-process
  executor (``semql_engine``) can handle these via raw-row streaming —
  call it via its own helper instead.

Single-backend queries should call :func:`compile_query` directly.
:func:`compile_federated_query` will also succeed on single-backend
queries (returning a degenerate one-fragment plan), but the simpler
type is preferred whenever federation isn't needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Literal

from semql.compile import ColumnMeta, Compiled, compile_query
from semql.errors import FederationError
from semql.introspect import resolve_query
from semql.model import Backend, Cube, Dimension, Join, Measure
from semql.spec import Filter, SemanticQuery, TimeWindow

FederationMode = Literal["distributive", "raw_rows"]
"""Federation compile-path selector.

- ``"distributive"`` (default) — fragments aggregate locally (SUM /
  COUNT / decomposed AVG); merge re-aggregates with SUM. Refuses
  ``count_distinct`` / ``min`` / ``max`` / ``ratio`` because they
  don't distribute under SUM.
- ``"raw_rows"`` — fragments emit ungrouped rows; merge applies the
  full aggregation. Lifts the non-distributive-agg restriction and
  the ``having`` restriction. Costs more bytes (full join cardinality
  on the wire) so callers should opt in deliberately.
"""

if TYPE_CHECKING:
    from semql.backend import BackendStrategy
    from semql.introspect import PolicyFn, ScopeFn
    from semql.model import AuthContext, View


# ---------------------------------------------------------------------------
# Public IR
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergePlan:
    """How to combine per-fragment result sets into the final result.

    ``sql`` references fragments as DuckDB tables named ``frag_0``,
    ``frag_1``, … indexed into ``FederatedPlan.fragments``. The
    executor (or any caller doing the merge manually) must materialise
    each fragment under that name before running ``sql``.
    """

    sql: str
    params: dict[str, object] = dc_field(default_factory=lambda: {})


@dataclass
class FederatedPlan:
    """A query that touches cubes on multiple backends.

    ``fragments[i]`` is a :class:`Compiled` to run against its
    respective backend. Results are loaded into a DuckDB table named
    ``frag_i`` and the ``merge.sql`` produces the final shape.

    ``columns`` and ``column_meta`` describe the final output shape
    after the merge — same role they play on :class:`Compiled`.
    """

    fragments: list[Compiled]
    merge: MergePlan
    columns: list[str]
    column_meta: list[ColumnMeta]


# ---------------------------------------------------------------------------
# Bridge join parsing — v1 supports only ``{a}.col = {b}.col`` shape.
# ---------------------------------------------------------------------------


_BRIDGE_RE = re.compile(
    r"^\s*\{(?P<la>[a-z_][a-z0-9_]*)\}\.(?P<lc>[a-z_][a-z0-9_]*)\s*"
    r"=\s*"
    r"\{(?P<ra>[a-z_][a-z0-9_]*)\}\.(?P<rc>[a-z_][a-z0-9_]*)\s*$"
)


@dataclass(frozen=True)
class _Bridge:
    """A cross-backend join edge with its keys extracted into structured form.

    ``left_cube`` is the cube the ``Join`` is declared on; ``right_cube``
    is its target. ``left_col`` / ``right_col`` are the column names on
    each side. ``left_dim`` / ``right_dim`` are the dimension names that
    expose those columns (the catalogue must declare them — see module
    docstring)."""

    left_cube: Cube
    right_cube: Cube
    left_col: str
    right_col: str
    left_dim: str
    right_dim: str


def _find_dim_for_column(cube: Cube, alias: str, column: str) -> str:
    """Find the dimension on ``cube`` whose ``sql`` exposes ``{alias}.column``.

    Used to translate a bridge join's column reference into a
    Dimension name the per-fragment sub-query can project. We compare
    the dimension's ``sql`` after substituting the alias — exact text
    match. Returns the dimension name. Raises :class:`FederationError`
    if no dimension exposes that column."""
    target = f"{alias}.{column}"
    for d in cube.dimensions:
        # Dimension SQL uses {alias} placeholders; substitute and trim
        # whitespace for the comparison. A user can write ``{o}.id`` or
        # ``{o}. id`` — normalise.
        rendered = d.sql.replace("{" + alias + "}", alias).strip()
        if rendered == target:
            return d.name
    raise FederationError(
        f"Cube {cube.name!r}: bridge join references column {target!r} "
        f"but no dimension on this cube exposes that column. Declare a "
        f"Dimension with sql={target!r} so the federation layer can "
        f"project the join key.",
        reason="join_key_not_a_dimension",
    )


def _parse_bridge(left_cube: Cube, right_cube: Cube, join: Join) -> _Bridge:
    """Parse a cross-backend ``Join`` into structured bridge keys.

    Refuses anything more complex than ``{a}.col = {b}.col``."""
    m = _BRIDGE_RE.match(join.on)
    if m is None:
        raise FederationError(
            f"Cross-backend join from {left_cube.name!r} to "
            f"{right_cube.name!r}: ``on`` must be a single column "
            f"equality of the form ``{{a}}.col = {{b}}.col``. Got: "
            f"{join.on!r}.",
            reason="bridge_join_not_simple_equality",
        )
    la, lc, ra, rc = m.group("la", "lc", "ra", "rc")
    if {la, ra} != {left_cube.alias, right_cube.alias}:
        raise FederationError(
            f"Cross-backend join from {left_cube.name!r} to "
            f"{right_cube.name!r}: aliases in ``on`` ({la!r}, {ra!r}) "
            f"don't match the cubes' aliases "
            f"({left_cube.alias!r}, {right_cube.alias!r}).",
            reason="bridge_alias_mismatch",
        )
    # Normalise so left/right match left_cube/right_cube.
    if la == right_cube.alias:
        la, lc, ra, rc = ra, rc, la, lc

    left_dim = _find_dim_for_column(left_cube, left_cube.alias, lc)
    right_dim = _find_dim_for_column(right_cube, right_cube.alias, rc)
    return _Bridge(
        left_cube=left_cube,
        right_cube=right_cube,
        left_col=lc,
        right_col=rc,
        left_dim=left_dim,
        right_dim=right_dim,
    )


# ---------------------------------------------------------------------------
# Partitioning + sub-query synthesis
# ---------------------------------------------------------------------------


def _touched(q: SemanticQuery, catalog: dict[str, Cube]) -> list[Cube]:
    resolved = resolve_query(q, catalog)
    return list(resolved.touched_cubes)


def _find_bridges(
    touched: list[Cube],
    catalog: dict[str, Cube],
) -> list[_Bridge]:
    """Walk every join declared on a touched cube; keep the ones that
    cross a backend boundary into another touched cube. A join's
    reverse direction is treated as the same bridge — we de-duplicate
    by ``(min, max)`` of the cube names."""
    in_scope = {c.name for c in touched}
    by_name = {c.name: c for c in touched}
    seen: set[tuple[str, str]] = set()
    bridges: list[_Bridge] = []
    for cube in touched:
        for j in cube.joins:
            if j.to not in in_scope:
                continue
            target = by_name[j.to]
            if target.backend == cube.backend:
                continue
            key = tuple(sorted((cube.name, target.name)))
            if key in seen:
                continue
            seen.add((key[0], key[1]))
            bridges.append(_parse_bridge(cube, target, j))
    return bridges


def _resolve_field_to_cube(ref: str, catalog: dict[str, Cube]) -> Cube:
    """Identify the cube that owns the field referenced by ``ref``.

    ``ref`` is the qualified ``cube.field`` form used in
    ``SemanticQuery.measures`` / ``dimensions`` / ``filters``."""
    if "." not in ref:
        raise FederationError(
            f"Reference {ref!r} must be qualified as ``cube.field``.",
            reason="unqualified_reference",
        )
    cube_name = ref.split(".", 1)[0]
    if cube_name not in catalog:
        raise FederationError(
            f"Reference {ref!r}: unknown cube {cube_name!r}.",
            reason="unknown_cube",
        )
    return catalog[cube_name]


_AVG_DECOMP_SUM_SUFFIX = "__avg_sum"
_AVG_DECOMP_COUNT_SUFFIX = "__avg_count"


def _decompose_avg_measure(m: Measure) -> tuple[Measure, Measure]:
    """For ``agg='avg'`` measures, emit a ``sum`` and a ``count`` pair
    the fragment can compute. The merge step recomposes the average as
    ``SUM(sum) / NULLIF(SUM(count), 0)``."""
    sum_m = m.model_copy(
        update={
            "name": m.name + _AVG_DECOMP_SUM_SUFFIX,
            "agg": "sum",
            "display_name": None,
            "format": None,
            "unit": m.unit,
            "display_unit": None,
        }
    )
    count_m = m.model_copy(
        update={
            "name": m.name + _AVG_DECOMP_COUNT_SUFFIX,
            "agg": "count",
            "display_name": None,
            "format": None,
            "unit": "count",
            "display_unit": None,
        }
    )
    return sum_m, count_m


@dataclass
class _PartitionPlan:
    """The per-backend sub-query we'll compile into a fragment.

    Carries enough metadata to drive the merge step: which dims are
    "projected" (visible in the final output), which are bridge keys
    (consumed by joins, not projected), which measure names came from
    avg-decomposition (need recomposing), and which measure names are
    pass-through.
    """

    backend: Backend
    cubes: list[Cube]
    sub_query: SemanticQuery
    # Map from the original measure ref ("orders.revenue") to the
    # column name(s) in the fragment's output.
    measure_columns: dict[str, str]
    # Original avg measures decomposed: ref → (sum_col, count_col).
    avg_columns: dict[str, tuple[str, str]]
    # Map from the original dim ref ("customers.region") to the column
    # name in the fragment's output. Only dims this partition owns.
    dim_columns: dict[str, str]
    # Bridge dim columns this fragment exposes (column names in the
    # fragment output). Keyed by the dim ref ("orders.customer_id")
    # so the merge step can wire them up.
    bridge_columns: dict[str, str]


def _build_partition_sub_query(
    q: SemanticQuery,
    catalog: dict[str, Cube],
    partition_cubes: list[Cube],
    primary_partition: Backend,
    bridges: list[_Bridge],
) -> _PartitionPlan:
    """Construct the ``SemanticQuery`` that will compile into this
    partition's fragment.

    Includes:
    - Dimensions this partition owns (from ``q.dimensions``).
    - Time dimension if it resolves to a cube in this partition.
    - Measures (only on the primary partition).
    - Filters whose dimension belongs to a cube in this partition.
    - Extra "bridge" dimensions for every join key on a bridge that
      touches a cube in this partition — so the merge step can join
      fragments on those columns.

    Refuses cross-partition where-trees, segments, and queries with
    measures on non-primary partitions.
    """
    partition_names = {c.name for c in partition_cubes}
    backend = partition_cubes[0].backend
    is_primary = backend is primary_partition

    sub_measures: list[str] = []
    measure_columns: dict[str, str] = {}
    avg_columns: dict[str, tuple[str, str]] = {}
    # Synthetic Measures we add to the cube for avg decomposition.
    synthetic_measures: dict[str, list[Measure]] = {}

    for ref in q.measures:
        owner = _resolve_field_to_cube(ref, catalog)
        if owner.name not in partition_names:
            if is_primary:
                # measure resolves to a non-primary partition — refused
                raise FederationError(
                    f"Measure {ref!r} resolves to cube {owner.name!r} on "
                    f"backend {owner.backend.value!r}, which is not the "
                    f"primary measure partition {primary_partition.value!r}. "
                    f"All measures must live on a single backend in v1; the "
                    f"in-process executor can lift this restriction.",
                    reason="measure_on_non_primary_partition",
                )
            continue
        # ref belongs to this partition.
        m_name = ref.rsplit(".", 1)[1]
        m = next(x for x in owner.measures if x.name == m_name)
        if m.agg in ("sum", "count"):
            sub_measures.append(ref)
            measure_columns[ref] = m_name
        elif m.agg == "avg":
            sum_m, count_m = _decompose_avg_measure(m)
            synthetic_measures.setdefault(owner.name, []).extend([sum_m, count_m])
            sub_measures.append(f"{owner.name}.{sum_m.name}")
            sub_measures.append(f"{owner.name}.{count_m.name}")
            avg_columns[ref] = (sum_m.name, count_m.name)
        else:
            raise FederationError(
                f"Measure {ref!r} uses agg={m.agg!r}, which is not "
                f"distributive across sources. v1 supports sum, count, "
                f"and avg; the in-process executor handles the rest via "
                f"raw-row streaming.",
                reason=f"non_distributive_aggregation:{m.agg}",
            )

    sub_dimensions: list[str] = []
    dim_columns: dict[str, str] = {}
    for ref in q.dimensions:
        owner = _resolve_field_to_cube(ref, catalog)
        if owner.name in partition_names:
            sub_dimensions.append(ref)
            dim_columns[ref] = ref.rsplit(".", 1)[1]

    # Time dimension: include if it resolves to this partition. The
    # time window itself stays on the fragment that owns the time dim
    # (it doesn't need to be re-applied at merge).
    sub_time_dim: TimeWindow | None = None
    if q.time_dimension is not None:
        td_cube = _resolve_field_to_cube(q.time_dimension.dimension, catalog)
        if td_cube.name in partition_names:
            sub_time_dim = q.time_dimension
            # Add the bucketed time column to dim_columns so the merge
            # step can group by it. The compiler aliases it
            # ``<td_name>_<granularity>`` when granularity is set, else
            # ``<td_name>``.
            td_name = q.time_dimension.dimension.rsplit(".", 1)[1]
            if q.time_dimension.granularity is not None:
                td_col = f"{td_name}_{q.time_dimension.granularity}"
            else:
                td_col = td_name
            dim_columns[q.time_dimension.dimension] = td_col

    # Filters whose dimension lives on this partition.
    sub_filters: list[Filter] = []
    for f in q.filters:
        owner = _resolve_field_to_cube(f.dimension, catalog)
        if owner.name in partition_names:
            sub_filters.append(f)

    # Bridge keys. For every bridge that touches a cube in this
    # partition, expose the appropriate side as a dimension.
    bridge_columns: dict[str, str] = {}
    for b in bridges:
        if b.left_cube.name in partition_names:
            ref = f"{b.left_cube.name}.{b.left_dim}"
            if ref not in sub_dimensions:
                sub_dimensions.append(ref)
            bridge_columns[ref] = b.left_dim
        if b.right_cube.name in partition_names:
            ref = f"{b.right_cube.name}.{b.right_dim}"
            if ref not in sub_dimensions:
                sub_dimensions.append(ref)
            bridge_columns[ref] = b.right_dim

    # If we synthesised measures (avg decomposition), splice them into
    # a model_copy of the owning cube so the compiler can resolve them.
    if synthetic_measures:
        for cube in list(partition_cubes):
            extra = synthetic_measures.get(cube.name)
            if not extra:
                continue
            patched = cube.model_copy(update={"measures": [*cube.measures, *extra]})
            # Replace in the partition list and let _scoped_catalog
            # pick up the patched version.
            partition_cubes[partition_cubes.index(cube)] = patched

    sub_query = SemanticQuery(
        measures=sub_measures,
        dimensions=sub_dimensions,
        time_dimension=sub_time_dim,
        filters=sub_filters,
        # No segments / where / having in v1 federated subqueries.
        order=[],
        # Ordering & limits happen at merge.
    )
    return _PartitionPlan(
        backend=backend,
        cubes=partition_cubes,
        sub_query=sub_query,
        measure_columns=measure_columns,
        avg_columns=avg_columns,
        dim_columns=dim_columns,
        bridge_columns=bridge_columns,
    )


def _scoped_catalog(partition_cubes: list[Cube]) -> dict[str, Cube]:
    """Return a catalog restricted to one partition's cubes, with
    intra-partition joins preserved and cross-partition joins stripped.

    The compiler will BFS the join graph during sub-query compilation
    and would otherwise stumble on joins pointing at cubes that aren't
    in this fragment's scope."""
    in_scope = {c.name for c in partition_cubes}
    scoped: dict[str, Cube] = {}
    for c in partition_cubes:
        local_joins = [j for j in c.joins if j.to in in_scope]
        if local_joins != list(c.joins):
            scoped[c.name] = c.model_copy(update={"joins": local_joins})
        else:
            scoped[c.name] = c
    return scoped


# ---------------------------------------------------------------------------
# Merge SQL emission
# ---------------------------------------------------------------------------


def _quote_ident(name: str) -> str:
    """DuckDB identifier quoting. Cube/dimension names already match
    ``[a-z_][a-z0-9_]*`` so quoting is conservative-but-safe."""
    return f'"{name}"'


def _emit_merge_sql(
    q: SemanticQuery,
    catalog: dict[str, Cube],
    primary_partition: Backend,
    partitions: list[_PartitionPlan],
    bridges: list[_Bridge],
    output_columns: list[str],
) -> str:
    """Generate the DuckDB merge SQL.

    Frags are aliased ``f0``, ``f1``, ... matching the partitions order.
    The primary fragment is the FROM target; every other fragment
    LEFT JOINs onto it via the appropriate bridge.
    """
    # Index the partitions by backend for join-key lookup.
    by_backend: dict[Backend, tuple[int, _PartitionPlan]] = {
        p.backend: (i, p) for i, p in enumerate(partitions)
    }
    primary_idx, _primary_plan = by_backend[primary_partition]

    def frag_alias(idx: int) -> str:
        return f"f{idx}"

    # Map cube → fragment index, so we know which alias to reference
    # for any (cube.field) ref in the final SELECT.
    cube_to_idx: dict[str, int] = {}
    for i, p in enumerate(partitions):
        for c in p.cubes:
            cube_to_idx[c.name] = i

    # SELECT list — one expression per output column the user asked for.
    select_exprs: list[str] = []
    for ref in q.dimensions:
        owner = _resolve_field_to_cube(ref, catalog)
        idx = cube_to_idx[owner.name]
        plan = partitions[idx]
        # The dim column in the fragment's output keeps its original
        # dim name (compiler emits the field name unless there's a
        # collision; cross-fragment we control naming).
        col = plan.dim_columns[ref]
        alias = ref.rsplit(".", 1)[1]
        select_exprs.append(f"{frag_alias(idx)}.{_quote_ident(col)} AS {_quote_ident(alias)}")

    if q.time_dimension is not None:
        td_cube = _resolve_field_to_cube(q.time_dimension.dimension, catalog)
        idx = cube_to_idx[td_cube.name]
        plan = partitions[idx]
        td_col = plan.dim_columns[q.time_dimension.dimension]
        # Output column name matches compile_query's convention.
        td_alias = td_col
        select_exprs.append(f"{frag_alias(idx)}.{_quote_ident(td_col)} AS {_quote_ident(td_alias)}")

    # Measures (re-aggregation).
    for ref in q.measures:
        owner = _resolve_field_to_cube(ref, catalog)
        idx = cube_to_idx[owner.name]
        plan = partitions[idx]
        m_name = ref.rsplit(".", 1)[1]
        if ref in plan.avg_columns:
            sum_col, count_col = plan.avg_columns[ref]
            expr = (
                f"SUM({frag_alias(idx)}.{_quote_ident(sum_col)}) / "
                f"NULLIF(SUM({frag_alias(idx)}.{_quote_ident(count_col)}), 0)"
            )
        else:
            col = plan.measure_columns[ref]
            # sum-of-sums and sum-of-counts both reduce to SUM at merge.
            expr = f"SUM({frag_alias(idx)}.{_quote_ident(col)})"
        select_exprs.append(f"{expr} AS {_quote_ident(m_name)}")

    # FROM + JOINs.
    from_clause = f"frag_{primary_idx} AS {frag_alias(primary_idx)}"
    joins_sql: list[str] = []
    joined: set[int] = {primary_idx}
    # Walk bridges in declaration order; each bridge connects exactly
    # one already-joined fragment to a not-yet-joined fragment.
    pending = list(bridges)
    while pending:
        progress = False
        remaining: list[_Bridge] = []
        for b in pending:
            left_idx = cube_to_idx[b.left_cube.name]
            right_idx = cube_to_idx[b.right_cube.name]
            if left_idx in joined and right_idx not in joined:
                new_idx = right_idx
                left_alias = frag_alias(left_idx)
                right_alias = frag_alias(new_idx)
                left_col = partitions[left_idx].bridge_columns[f"{b.left_cube.name}.{b.left_dim}"]
                right_col = partitions[new_idx].bridge_columns[f"{b.right_cube.name}.{b.right_dim}"]
            elif right_idx in joined and left_idx not in joined:
                new_idx = left_idx
                left_alias = frag_alias(right_idx)
                right_alias = frag_alias(new_idx)
                left_col = partitions[right_idx].bridge_columns[
                    f"{b.right_cube.name}.{b.right_dim}"
                ]
                right_col = partitions[new_idx].bridge_columns[f"{b.left_cube.name}.{b.left_dim}"]
            else:
                remaining.append(b)
                continue
            joins_sql.append(
                f"LEFT JOIN frag_{new_idx} AS {right_alias} "
                f"ON {left_alias}.{_quote_ident(left_col)} = "
                f"{right_alias}.{_quote_ident(right_col)}"
            )
            joined.add(new_idx)
            progress = True
        pending = remaining
        if not progress and pending:
            unreached = [f"{b.left_cube.name}<->{b.right_cube.name}" for b in pending]
            raise FederationError(
                "Federated plan has disconnected backend partitions — "
                "the bridge join graph is not a connected tree rooted at "
                f"the primary partition. Unreached bridges: {unreached}. "
                "Add a join from a primary-partition cube to each "
                "satellite, or run them as separate queries.",
                reason="disconnected_partitions",
            )

    # GROUP BY all non-measure SELECT items.
    n_group = len(q.dimensions) + (1 if q.time_dimension is not None else 0)
    group_by = ", ".join(str(i + 1) for i in range(n_group))

    sql = f"SELECT {', '.join(select_exprs)} FROM {from_clause}"
    if joins_sql:
        sql += " " + " ".join(joins_sql)
    if q.measures and n_group > 0:
        sql += f" GROUP BY {group_by}"

    # ORDER BY + LIMIT/OFFSET — apply against output column aliases.
    if q.order:
        order_terms: list[str] = []
        for ref, direction in q.order:
            # Resolve output column name.
            alias = ref.rsplit(".", 1)[-1] if "." in ref else ref
            if alias not in output_columns:
                raise FederationError(
                    f"ORDER BY {ref!r}: not in the federated output columns {output_columns}.",
                    reason="order_by_unknown_column",
                )
            order_terms.append(f"{_quote_ident(alias)} {'DESC' if direction == 'desc' else 'ASC'}")
        sql += f" ORDER BY {', '.join(order_terms)}"

    if q.limit is not None:
        sql += f" LIMIT {int(q.limit)}"
    if q.offset is not None and q.offset > 0:
        sql += f" OFFSET {int(q.offset)}"

    return sql


# ---------------------------------------------------------------------------
# Raw-row mode — lifts non-distributive aggregations and having
# ---------------------------------------------------------------------------


# Synthetic-dim name prefix for a measure's raw source SQL in raw_rows mode.
# Picked so it can't collide with a user-declared dimension under semql's
# ``[a-z_][a-z0-9_]*`` naming rules.
_RAW_MEASURE_PREFIX = "__rm_"


def _raw_measure_col(measure_name: str) -> str:
    """Output-column name carrying a measure's raw source value."""
    return _RAW_MEASURE_PREFIX + measure_name


@dataclass
class _RawRowPartitionPlan:
    """Per-backend sub-query for raw-row federation.

    Mirrors :class:`_PartitionPlan` but the sub-query is ``ungrouped=True``
    — no aggregation in the fragment — and the merge step does all the
    work. ``raw_measure_columns`` maps each original measure ref to the
    column name + aggregator the merge SQL should apply.
    """

    backend: Backend
    cubes: list[Cube]
    sub_query: SemanticQuery
    # Map measure ref ("orders.revenue") to (raw_col_name, agg). When
    # ``raw_col_name`` is empty the merge should use ``COUNT(*)``
    # rather than aggregating a column (the case for ``COUNT(*)``
    # measures that have no underlying column).
    raw_measure_columns: dict[str, tuple[str, str]]
    dim_columns: dict[str, str]
    bridge_columns: dict[str, str]


def _build_partition_sub_query_raw_rows(
    q: SemanticQuery,
    catalog: dict[str, Cube],
    partition_cubes: list[Cube],
    primary_partition: Backend,
    bridges: list[_Bridge],
) -> _RawRowPartitionPlan:
    """Construct an ungrouped per-partition fragment for raw-row mode.

    Selects: every output dim this partition owns + every bridge key
    that touches a partition cube + a synthetic dimension exposing
    each measure's raw source SQL. The compiler runs with
    ``_allow_unbounded_ungrouped=True`` because raw-row federation
    inherently produces fragment-cardinality rows.

    Refuses (in raw-row mode): ratio measures (would need recursive
    expansion of numerator/denominator into raw cols — follow-up),
    filtered measures (would need NULL-projection over the filter
    predicate — follow-up).
    """
    partition_names = {c.name for c in partition_cubes}
    backend = partition_cubes[0].backend
    is_primary = backend is primary_partition

    raw_measure_columns: dict[str, tuple[str, str]] = {}
    synthetic_dims: dict[str, list[Dimension]] = {}

    for ref in q.measures:
        owner = _resolve_field_to_cube(ref, catalog)
        if owner.name not in partition_names:
            if is_primary:
                raise FederationError(
                    f"Measure {ref!r} resolves to cube {owner.name!r} on "
                    f"backend {owner.backend.value!r}, which is not the "
                    f"primary measure partition {primary_partition.value!r}. "
                    f"All measures must live on one backend; the engine "
                    f"can't cross-source-join row-level measure values "
                    f"yet.",
                    reason="measure_on_non_primary_partition",
                )
            continue
        m_name = ref.rsplit(".", 1)[1]
        m = next(x for x in owner.measures if x.name == m_name)
        if m.agg == "ratio":
            raise FederationError(
                f"Measure {ref!r} uses agg='ratio' which is not yet "
                "supported in raw_rows federation mode. Use distributive "
                "mode (if numerator+denominator are sum/count) or expand "
                "the ratio in the SemanticQuery and combine client-side.",
                reason="ratio_in_raw_rows",
            )
        if m.filter:
            raise FederationError(
                f"Measure {ref!r} declares ``filter=...`` which is not "
                "yet supported in raw_rows federation mode.",
                reason="filtered_measure_in_raw_rows",
            )
        if m.agg == "count" and m.sql.strip() == "*":
            # COUNT(*) — no raw column; merge emits COUNT(*) over the
            # joined rows.
            raw_measure_columns[ref] = ("", "count")
            continue
        raw_col = _raw_measure_col(m_name)
        raw_dim = Dimension(name=raw_col, sql=m.sql, type="number")
        synthetic_dims.setdefault(owner.name, []).append(raw_dim)
        raw_measure_columns[ref] = (raw_col, m.agg)

    if synthetic_dims:
        for cube in list(partition_cubes):
            extra = synthetic_dims.get(cube.name)
            if not extra:
                continue
            patched = cube.model_copy(update={"dimensions": [*cube.dimensions, *extra]})
            partition_cubes[partition_cubes.index(cube)] = patched

    sub_dimensions: list[str] = []
    dim_columns: dict[str, str] = {}
    for ref in q.dimensions:
        owner = _resolve_field_to_cube(ref, catalog)
        if owner.name in partition_names:
            sub_dimensions.append(ref)
            dim_columns[ref] = ref.rsplit(".", 1)[1]

    if q.time_dimension is not None:
        td_cube = _resolve_field_to_cube(q.time_dimension.dimension, catalog)
        if td_cube.name in partition_names:
            raise FederationError(
                "raw_rows federation mode does not yet support "
                "time_dimension — the merge step would need to apply "
                "the granularity bucket to raw timestamps, which is a "
                "follow-up. Use distributive mode, or pre-bucket the "
                "time column as a Dimension.",
                reason="time_dimension_in_raw_rows",
            )

    sub_filters: list[Filter] = []
    for f in q.filters:
        owner = _resolve_field_to_cube(f.dimension, catalog)
        if owner.name in partition_names:
            sub_filters.append(f)

    bridge_columns: dict[str, str] = {}
    for b in bridges:
        if b.left_cube.name in partition_names:
            ref = f"{b.left_cube.name}.{b.left_dim}"
            if ref not in sub_dimensions:
                sub_dimensions.append(ref)
            bridge_columns[ref] = b.left_dim
        if b.right_cube.name in partition_names:
            ref = f"{b.right_cube.name}.{b.right_dim}"
            if ref not in sub_dimensions:
                sub_dimensions.append(ref)
            bridge_columns[ref] = b.right_dim

    # Project synthetic raw-measure dims (skip the synthetic-less
    # COUNT(*) cases).
    for ref, (col, _agg) in raw_measure_columns.items():
        if not col:
            continue
        owner = _resolve_field_to_cube(ref, catalog)
        sub_dimensions.append(f"{owner.name}.{col}")

    sub_query = SemanticQuery(
        measures=[],
        dimensions=sub_dimensions,
        filters=sub_filters,
        ungrouped=True,
        order=[],
    )
    return _RawRowPartitionPlan(
        backend=backend,
        cubes=partition_cubes,
        sub_query=sub_query,
        raw_measure_columns=raw_measure_columns,
        dim_columns=dim_columns,
        bridge_columns=bridge_columns,
    )


def _emit_merge_sql_raw_rows(
    q: SemanticQuery,
    catalog: dict[str, Cube],
    primary_partition: Backend,
    partitions: list[_RawRowPartitionPlan],
    bridges: list[_Bridge],
    output_columns: list[str],
) -> str:
    """Generate the DuckDB merge SQL for raw-row federation.

    Structure: FROM primary fragment LEFT JOIN satellites on bridge
    columns, GROUP BY output dims, SELECT dims + per-measure
    aggregation expression. ``having`` (if any) lives at this layer
    against the recomposed measure aliases — that's how raw-row mode
    lifts the v1 ``having`` refusal.
    """
    by_backend: dict[Backend, tuple[int, _RawRowPartitionPlan]] = {
        p.backend: (i, p) for i, p in enumerate(partitions)
    }
    primary_idx, _primary_plan = by_backend[primary_partition]

    def frag_alias(idx: int) -> str:
        return f"f{idx}"

    cube_to_idx: dict[str, int] = {}
    for i, p in enumerate(partitions):
        for c in p.cubes:
            cube_to_idx[c.name] = i

    select_exprs: list[str] = []
    for ref in q.dimensions:
        owner = _resolve_field_to_cube(ref, catalog)
        idx = cube_to_idx[owner.name]
        plan = partitions[idx]
        col = plan.dim_columns[ref]
        alias = ref.rsplit(".", 1)[1]
        select_exprs.append(f"{frag_alias(idx)}.{_quote_ident(col)} AS {_quote_ident(alias)}")

    # Per-measure aggregation in the merge — the heart of raw-row mode.
    for ref in q.measures:
        owner = _resolve_field_to_cube(ref, catalog)
        idx = cube_to_idx[owner.name]
        plan = partitions[idx]
        m_name = ref.rsplit(".", 1)[1]
        col, agg = plan.raw_measure_columns[ref]
        if not col:
            # COUNT(*) over the joined rows.
            expr = "COUNT(*)"
        else:
            ref_col = f"{frag_alias(idx)}.{_quote_ident(col)}"
            if agg == "sum":
                expr = f"SUM({ref_col})"
            elif agg == "count":
                expr = f"COUNT({ref_col})"
            elif agg == "count_distinct":
                expr = f"COUNT(DISTINCT {ref_col})"
            elif agg == "avg":
                expr = f"AVG({ref_col})"
            elif agg == "min":
                expr = f"MIN({ref_col})"
            elif agg == "max":
                expr = f"MAX({ref_col})"
            else:
                # Defensive — partition builder refuses the rest.
                raise FederationError(
                    f"Measure {ref!r} uses agg={agg!r} which is not implemented in raw_rows merge.",
                    reason=f"unimplemented_agg_in_raw_rows:{agg}",
                )
        select_exprs.append(f"{expr} AS {_quote_ident(m_name)}")

    # FROM + LEFT JOIN chain — identical shape to the distributive path.
    from_clause = f"frag_{primary_idx} AS {frag_alias(primary_idx)}"
    joins_sql: list[str] = []
    joined: set[int] = {primary_idx}
    pending = list(bridges)
    while pending:
        progress = False
        remaining: list[_Bridge] = []
        for b in pending:
            left_idx = cube_to_idx[b.left_cube.name]
            right_idx = cube_to_idx[b.right_cube.name]
            if left_idx in joined and right_idx not in joined:
                new_idx = right_idx
                left_alias = frag_alias(left_idx)
                right_alias = frag_alias(new_idx)
                left_col = partitions[left_idx].bridge_columns[f"{b.left_cube.name}.{b.left_dim}"]
                right_col = partitions[new_idx].bridge_columns[f"{b.right_cube.name}.{b.right_dim}"]
            elif right_idx in joined and left_idx not in joined:
                new_idx = left_idx
                left_alias = frag_alias(right_idx)
                right_alias = frag_alias(new_idx)
                left_col = partitions[right_idx].bridge_columns[
                    f"{b.right_cube.name}.{b.right_dim}"
                ]
                right_col = partitions[new_idx].bridge_columns[f"{b.left_cube.name}.{b.left_dim}"]
            else:
                remaining.append(b)
                continue
            joins_sql.append(
                f"LEFT JOIN frag_{new_idx} AS {right_alias} "
                f"ON {left_alias}.{_quote_ident(left_col)} = "
                f"{right_alias}.{_quote_ident(right_col)}"
            )
            joined.add(new_idx)
            progress = True
        pending = remaining
        if not progress and pending:
            unreached = [f"{b.left_cube.name}<->{b.right_cube.name}" for b in pending]
            raise FederationError(
                "Federated plan has disconnected backend partitions — "
                "the bridge join graph is not a connected tree rooted at "
                f"the primary partition. Unreached bridges: {unreached}.",
                reason="disconnected_partitions",
            )

    n_group = len(q.dimensions)
    group_by = ", ".join(str(i + 1) for i in range(n_group))

    sql = f"SELECT {', '.join(select_exprs)} FROM {from_clause}"
    if joins_sql:
        sql += " " + " ".join(joins_sql)
    if q.measures and n_group > 0:
        sql += f" GROUP BY {group_by}"

    # HAVING — raw-row mode applies it at merge against the recomposed
    # measure aliases. Each Filter's ``dimension`` must name one of the
    # measure refs in ``q.measures``; we resolve to the output alias.
    if q.having:
        having_terms: list[str] = []
        for hf in q.having:
            if hf.dimension not in q.measures:
                raise FederationError(
                    f"HAVING dimension {hf.dimension!r} in raw_rows mode "
                    "must reference one of the query's measures by "
                    f"qualified name. Known measures: {q.measures}.",
                    reason="having_unknown_measure",
                )
            alias = hf.dimension.rsplit(".", 1)[1]
            having_terms.append(_emit_having_term(alias, hf))
        sql += f" HAVING {' AND '.join(having_terms)}"

    if q.order:
        order_terms: list[str] = []
        for ref, direction in q.order:
            alias = ref.rsplit(".", 1)[-1] if "." in ref else ref
            if alias not in output_columns:
                raise FederationError(
                    f"ORDER BY {ref!r}: not in the federated output columns {output_columns}.",
                    reason="order_by_unknown_column",
                )
            order_terms.append(f"{_quote_ident(alias)} {'DESC' if direction == 'desc' else 'ASC'}")
        sql += f" ORDER BY {', '.join(order_terms)}"

    if q.limit is not None:
        sql += f" LIMIT {int(q.limit)}"
    if q.offset is not None and q.offset > 0:
        sql += f" OFFSET {int(q.offset)}"

    return sql


def _emit_having_term(alias: str, f: Filter) -> str:
    """Render a single HAVING predicate in DuckDB dialect.

    Used by raw_rows mode; the alias references a measure output column
    on the merge SELECT. Values inline because the merge SQL is a
    single string with no separate params envelope — and the values
    come from the user-supplied SemanticQuery's HAVING filters which
    were validated upstream."""
    col = _quote_ident(alias)
    op = f.op
    vals = list(f.values)
    if op == "gt":
        return f"{col} > {_lit(vals[0])}"
    if op == "gte":
        return f"{col} >= {_lit(vals[0])}"
    if op == "lt":
        return f"{col} < {_lit(vals[0])}"
    if op == "lte":
        return f"{col} <= {_lit(vals[0])}"
    if op == "eq":
        return f"{col} = {_lit(vals[0])}"
    if op == "neq":
        return f"{col} <> {_lit(vals[0])}"
    raise FederationError(
        f"HAVING operator {op!r} is not yet supported in raw_rows federation mode.",
        reason=f"unsupported_having_op:{op}",
    )


def _lit(v: object) -> str:
    """Emit a literal in DuckDB dialect — numeric or single-quoted string."""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def _compile_raw_rows(
    q: SemanticQuery,
    catalog: dict[str, Cube],
    grouped: dict[Backend, list[Cube]],
    backend_order: list[Backend],
    primary_partition: Backend,
    bridges: list[_Bridge],
    *,
    context: dict[str, str] | None,
    group_by_alias: bool,
    having_alias: bool,
    strategies: dict[Backend, BackendStrategy] | None,
    viewer: AuthContext | None,
    policy: PolicyFn | None,
    scope_fns: dict[str, ScopeFn] | None,
) -> FederatedPlan:
    """Multi-backend raw-row compile path.

    Each partition emits an ungrouped fragment (raw rows + bridge keys
    + synthetic raw-measure dims); the merge step joins them and
    applies the final aggregation (including non-distributive aggs and
    ``having``)."""
    partitions: list[_RawRowPartitionPlan] = []
    for backend in backend_order:
        plan = _build_partition_sub_query_raw_rows(
            q, catalog, list(grouped[backend]), primary_partition, bridges
        )
        partitions.append(plan)

    fragments: list[Compiled] = []
    for plan in partitions:
        scoped = _scoped_catalog(plan.cubes)
        c = compile_query(
            plan.sub_query,
            scoped,
            context=context,
            group_by_alias=group_by_alias,
            having_alias=having_alias,
            strategies=strategies,
            views=None,
            viewer=viewer,
            policy=policy,
            scope_fns=scope_fns,
            _allow_unbounded_ungrouped=True,
        )
        fragments.append(c)

    output_columns: list[str] = []
    output_column_meta: list[ColumnMeta] = []
    cube_to_idx: dict[str, int] = {}
    for i, p in enumerate(partitions):
        for cube in p.cubes:
            cube_to_idx[cube.name] = i

    def _meta_for_dim(ref: str) -> ColumnMeta:
        owner = _resolve_field_to_cube(ref, catalog)
        idx = cube_to_idx[owner.name]
        plan = partitions[idx]
        col = plan.dim_columns[ref]
        for cm in fragments[idx].column_meta:
            if cm.name == col:
                return ColumnMeta(
                    name=ref.rsplit(".", 1)[1],
                    kind=cm.kind,
                    display_name=cm.display_name,
                    unit=cm.unit,
                    display_unit=cm.display_unit,
                    format=cm.format,
                )
        return ColumnMeta(name=ref.rsplit(".", 1)[1], kind="dimension")

    def _meta_for_measure(ref: str) -> ColumnMeta:
        owner = _resolve_field_to_cube(ref, catalog)
        m_name = ref.rsplit(".", 1)[1]
        m = next(x for x in owner.measures if x.name == m_name)
        return ColumnMeta(
            name=m_name,
            kind="measure",
            display_name=m.display_name or m_name.replace("_", " ").title(),
            unit=m.unit,
            display_unit=m.display_unit,
            format=m.format,
        )

    for ref in q.dimensions:
        output_columns.append(ref.rsplit(".", 1)[1])
        output_column_meta.append(_meta_for_dim(ref))

    for ref in q.measures:
        output_columns.append(ref.rsplit(".", 1)[1])
        output_column_meta.append(_meta_for_measure(ref))

    merge_sql = _emit_merge_sql_raw_rows(
        q, catalog, primary_partition, partitions, bridges, output_columns
    )
    return FederatedPlan(
        fragments=fragments,
        merge=MergePlan(sql=merge_sql),
        columns=output_columns,
        column_meta=output_column_meta,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def compile_federated_query(
    q: SemanticQuery,
    catalog: dict[str, Cube],
    *,
    context: dict[str, str] | None = None,
    group_by_alias: bool = True,
    having_alias: bool = False,
    strategies: dict[Backend, BackendStrategy] | None = None,
    views: dict[str, View] | None = None,
    viewer: AuthContext | None = None,
    policy: PolicyFn | None = None,
    scope_fns: dict[str, ScopeFn] | None = None,
    mode: FederationMode = "distributive",
) -> FederatedPlan:
    """Compile a query whose touched cubes span multiple backends.

    Returns a :class:`FederatedPlan` with one :class:`Compiled`
    fragment per backend and a DuckDB merge SQL that joins them.

    Single-backend queries succeed too — the returned plan has a single
    fragment and a trivial merge — but :func:`compile_query` is the
    simpler API for that case.

    See the module docstring for the v1 restriction set. Refusals are
    :class:`FederationError`\\s with a structured ``reason`` attribute
    for programmatic handling.
    """
    if q.compare is not None:
        raise FederationError(
            "Federated compare-mode is not supported in v1. Run the "
            "current and prior windows as separate federated queries "
            "and diff client-side, or use the in-process executor.",
            reason="compare_in_federated",
        )
    if q.where is not None:
        raise FederationError(
            "Federated queries cannot use ``where`` (the boolean "
            "predicate tree) in v1 — partitioning OR / NOT trees across "
            "backends is ambiguous. Use flat ``filters`` instead; each "
            "Filter routes to the partition of the field it references.",
            reason="where_tree_in_federated",
        )
    if q.segments:
        raise FederationError(
            "Federated queries cannot reference segments in v1. Inline "
            "the segment's predicate as a flat Filter on the same cube.",
            reason="segments_in_federated",
        )
    if q.having and mode == "distributive":
        raise FederationError(
            "Federated queries cannot use HAVING in distributive mode "
            "— the having predicate would have to run against "
            "re-aggregated measures at the merge step, which raw_rows "
            'mode now lifts. Pass ``mode="raw_rows"`` to '
            "compile_federated_query.",
            reason="having_in_distributive_federated",
        )

    touched = _touched(q, catalog)
    if not touched:
        raise FederationError(
            "Query is empty — no cubes touched. Federation needs at least one cube reference.",
            reason="empty_query",
        )
    backends_seen = {c.backend for c in touched}

    # Degenerate single-backend case: delegate to compile_query and wrap.
    if len(backends_seen) == 1:
        c = compile_query(
            q,
            catalog,
            context=context,
            group_by_alias=group_by_alias,
            having_alias=having_alias,
            strategies=strategies,
            views=views,
            viewer=viewer,
            policy=policy,
            scope_fns=scope_fns,
        )
        return FederatedPlan(
            fragments=[c],
            merge=MergePlan(sql="SELECT * FROM frag_0"),
            columns=c.columns,
            column_meta=c.column_meta,
        )

    # Pick the primary partition — the backend that owns any measure.
    # If there are no measures, the primary is the backend of the first
    # touched cube (deterministic; doesn't affect correctness).
    primary_partition: Backend
    if q.measures:
        m_owner = _resolve_field_to_cube(q.measures[0], catalog)
        primary_partition = m_owner.backend
        # Refuse if any other measure resolves to a different backend.
        for ref in q.measures[1:]:
            owner = _resolve_field_to_cube(ref, catalog)
            if owner.backend is not primary_partition:
                raise FederationError(
                    f"Measures span multiple backends: {ref!r} resolves "
                    f"to {owner.backend.value!r} but the primary "
                    f"partition (from the first measure) is "
                    f"{primary_partition.value!r}. v1 federated queries "
                    f"require all measures to live on one backend.",
                    reason="measures_span_backends",
                )
    else:
        primary_partition = touched[0].backend

    # Find every cross-backend bridge.
    bridges = _find_bridges(touched, catalog)
    if not bridges:
        raise FederationError(
            "Touched cubes span multiple backends but the catalogue "
            "declares no cross-backend Join between them. Add a "
            "many_to_one Join (or a foreign_key dimension that "
            "auto-derives one).",
            reason="no_cross_backend_join",
        )

    # Group cubes by backend (preserving first-mention order).
    grouped: dict[Backend, list[Cube]] = {}
    for cube in touched:
        grouped.setdefault(cube.backend, []).append(cube)

    # Build per-partition sub-queries. We sort so the primary partition
    # comes first — the merge SQL FROMs the primary and LEFT JOINs the
    # satellites.
    backend_order = [primary_partition] + [b for b in grouped if b is not primary_partition]

    if mode == "raw_rows":
        return _compile_raw_rows(
            q,
            catalog,
            grouped,
            backend_order,
            primary_partition,
            bridges,
            context=context,
            group_by_alias=group_by_alias,
            having_alias=having_alias,
            strategies=strategies,
            viewer=viewer,
            policy=policy,
            scope_fns=scope_fns,
        )

    partitions: list[_PartitionPlan] = []
    for backend in backend_order:
        plan = _build_partition_sub_query(
            q, catalog, list(grouped[backend]), primary_partition, bridges
        )
        partitions.append(plan)

    # Compile each fragment.
    fragments: list[Compiled] = []
    for plan in partitions:
        scoped = _scoped_catalog(plan.cubes)
        c = compile_query(
            plan.sub_query,
            scoped,
            context=context,
            group_by_alias=group_by_alias,
            having_alias=having_alias,
            strategies=strategies,
            views=None,
            viewer=viewer,
            policy=policy,
            scope_fns=scope_fns,
        )
        fragments.append(c)

    # Compose output columns + column_meta.
    output_columns: list[str] = []
    output_column_meta: list[ColumnMeta] = []
    cube_to_idx: dict[str, int] = {}
    for i, p in enumerate(partitions):
        for cube in p.cubes:
            cube_to_idx[cube.name] = i

    def _meta_for_dim(ref: str) -> ColumnMeta:
        owner = _resolve_field_to_cube(ref, catalog)
        idx = cube_to_idx[owner.name]
        plan = partitions[idx]
        col = plan.dim_columns[ref]
        for cm in fragments[idx].column_meta:
            if cm.name == col:
                # Re-name to the final output column name (no cube prefix).
                return ColumnMeta(
                    name=ref.rsplit(".", 1)[1],
                    kind=cm.kind,
                    display_name=cm.display_name,
                    unit=cm.unit,
                    display_unit=cm.display_unit,
                    format=cm.format,
                )
        return ColumnMeta(name=ref.rsplit(".", 1)[1], kind="dimension")

    def _meta_for_measure(ref: str) -> ColumnMeta:
        owner = _resolve_field_to_cube(ref, catalog)
        m_name = ref.rsplit(".", 1)[1]
        # Pull display info from the original Measure declaration.
        m = next(x for x in owner.measures if x.name == m_name)
        return ColumnMeta(
            name=m_name,
            kind="measure",
            display_name=m.display_name or m_name.replace("_", " ").title(),
            unit=m.unit,
            display_unit=m.display_unit,
            format=m.format,
        )

    for ref in q.dimensions:
        output_columns.append(ref.rsplit(".", 1)[1])
        output_column_meta.append(_meta_for_dim(ref))

    if q.time_dimension is not None:
        td_cube = _resolve_field_to_cube(q.time_dimension.dimension, catalog)
        idx = cube_to_idx[td_cube.name]
        td_col = partitions[idx].dim_columns[q.time_dimension.dimension]
        output_columns.append(td_col)
        output_column_meta.append(_meta_for_dim(q.time_dimension.dimension))

    for ref in q.measures:
        output_columns.append(ref.rsplit(".", 1)[1])
        output_column_meta.append(_meta_for_measure(ref))

    merge_sql = _emit_merge_sql(q, catalog, primary_partition, partitions, bridges, output_columns)
    return FederatedPlan(
        fragments=fragments,
        merge=MergePlan(sql=merge_sql),
        columns=output_columns,
        column_meta=output_column_meta,
    )


__all__ = [
    "FederatedPlan",
    "FederationError",
    "FederationMode",
    "MergePlan",
    "compile_federated_query",
]
