from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from semql._resolve import _ResolvedFields, walk_query_fields
from semql.errors import CompileError, JoinPathError
from semql.model import Cube, View
from semql.model import Join as ModelJoin
from semql.spec import SemanticQuery


@dataclass(frozen=True)
class Scan:
    cube: Cube
    alias: str


@dataclass(frozen=True)
class Join:
    left: Cube
    right: Cube
    on: str
    kind: Literal["inner", "left"]
    model: ModelJoin


@dataclass(frozen=True)
class Filter:
    expr: str


@dataclass(frozen=True)
class Aggregate:
    group_by: list[str]
    measures: list[str]


@dataclass(frozen=True)
class Project:
    columns: list[tuple[str, str]]  # (sql, alias)


@dataclass(frozen=True)
class LogicalPlan:
    scans: list[Scan]
    joins: list[Join]
    filters: list[Filter]
    aggregate: Aggregate | None
    project: Project
    touched: list[Cube]
    root: Cube


def to_logical_plan(
    query: SemanticQuery,
    catalog: dict[str, Cube],
    *,
    views: dict[str, View] | None = None,
    resolved: _ResolvedFields | None = None,
) -> LogicalPlan:
    """Lower a SemanticQuery to a LogicalPlan IR.

    This stage handles field resolution, join path discovery, and
    logical structure (aggregation vs ungrouped), but does NOT emit
    backend-specific SQL.
    """
    if resolved is None:
        views_map = views or {}
        resolved, diagnostics = walk_query_fields(query, catalog, views_map=views_map)
        if diagnostics:
            # For simplicity in this architectural refactor, we re-use the
            # diagnostic reporting style from compile.py.
            lines = [f"  - {d.message}" for d in diagnostics]
            raise CompileError(
                f"SemanticQuery has {len(diagnostics)} resolution errors:\n" + "\n".join(lines)
            )

    if not resolved.touched:
        raise CompileError("Could not determine any cubes from the query.")

    # 1. Join graph
    left_join_cubes = set(query.left_joins)
    cubes_in_from, join_edges = build_join_graph(
        resolved.touched, catalog, left_join_cubes=left_join_cubes
    )

    # 2. Scans & Joins
    scans = [Scan(cube=c, alias=c.alias) for c in cubes_in_from]
    joins: list[Join] = []
    for left, right, j in join_edges:
        joins.append(
            Join(
                left=left,
                right=right,
                on=j.on,
                kind="left" if right.name in left_join_cubes else "inner",
                model=j,
            )
        )

    # 3. Filters
    # (In a real implementation, we'd lower SpecFilter to a logical Filter expr.
    # For now, we'll keep it simple and just record that we have filters.)
    # TODO: Proper filter lowering
    filters = [Filter(expr=str(f)) for f in query.filters]

    # 4. Aggregate
    aggregate = None
    if not query.ungrouped:
        aggregate = Aggregate(
            group_by=[d for d in query.dimensions],
            measures=[m for m in query.measures],
        )

    # 5. Project
    # (Simplified: just project requested fields)
    project_cols: list[tuple[str, str]] = []
    for d in query.dimensions:
        project_cols.append((d, d.split(".")[-1]))
    for m in query.measures:
        project_cols.append((m, m.split(".")[-1]))

    project = Project(columns=project_cols)

    return LogicalPlan(
        scans=scans,
        joins=joins,
        filters=filters,
        aggregate=aggregate,
        project=project,
        touched=resolved.touched,
        root=cubes_in_from[0],
    )


def build_join_graph(
    touched: list[Cube],
    catalog: dict[str, Cube],
    *,
    left_join_cubes: set[str] | None = None,
) -> tuple[list[Cube], list[tuple[Cube, Cube, ModelJoin]]]:
    left_set: set[str] = left_join_cubes or set()
    root = touched[0]
    join_edges: list[tuple[Cube, Cube, ModelJoin]] = []
    cubes_in_from: list[Cube] = [root]
    for c in touched:
        if c is root:
            continue
        path = find_join_path(
            root.name,
            c.name,
            catalog,
            bidirectional=c.name in left_set,
        )
        cursor = root
        for next_name, j in path:
            tgt = catalog[next_name]
            if tgt not in cubes_in_from:
                join_edges.append((cursor, tgt, j))
                cubes_in_from.append(tgt)
            cursor = tgt
    return cubes_in_from, join_edges


def find_join_path(
    root: str,
    target: str,
    catalog: dict[str, Cube],
    *,
    bidirectional: bool = False,
) -> list[tuple[str, ModelJoin]]:
    if root == target:
        return []
    visited: set[str] = {root}
    queue: list[tuple[str, list[tuple[str, ModelJoin]]]] = [(root, [])]
    while queue:
        current, path = queue.pop(0)
        for j in catalog[current].joins:
            if j.to in visited:
                continue
            new_path = path + [(j.to, j)]
            if j.to == target:
                return new_path
            visited.add(j.to)
            queue.append((j.to, new_path))
        if bidirectional:
            for other_name, other_cube in catalog.items():
                if other_name in visited:
                    continue
                for j in other_cube.joins:
                    if j.to != current:
                        continue
                    new_path = path + [(other_name, j)]
                    if other_name == target:
                        return new_path
                    visited.add(other_name)
                    queue.append((other_name, new_path))
    raise JoinPathError(
        f"No join path from cube {root!r} to {target!r}. "
        "Declare a Join in the catalog or restructure the query.",
        root_cube=root,
        target_cube=target,
    )
