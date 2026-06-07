"""The Catalog wrapper — one object that owns a list of cubes.

`Catalog` is the high-level API people import. It validates the cube
graph at construction time, auto-appends the reflection META cubes, and
provides convenience methods that wrap the lower-level
``compile_query`` and ``build_planner_prompt_fragment`` functions.

Construction-time validation:
- No duplicate cube names.
- Every ``Join.to`` resolves to a cube in the catalogue.

Both are reasons a query would fail at compile time later — surfacing
them at catalogue construction means the planner and MCP layer can
trust the input.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, TypeVar

from semql.introspect import META_CUBES, PolicyFn
from semql.model import AuthContext, BaseField, Cube, Join, View

_T = TypeVar("_T", bound=BaseField)

if TYPE_CHECKING:
    from semql.compile import Compiled
    from semql.spec import SemanticQuery


class Catalog:
    """A validated collection of cubes plus the convenience surface
    (``compile``, ``prompt``, ``as_dict``) downstream code wants."""

    def __init__(
        self,
        cubes: list[Cube],
        *,
        views: list[View] | None = None,
        policy: PolicyFn | None = None,
    ) -> None:
        names = [c.name for c in cubes]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(
                f"Catalog has duplicate cube names: {duplicates}. "
                "Each cube.name must be unique within a catalog."
            )

        # Auto-append any missing META cubes so reflection always works.
        existing = set(names)
        merged: list[Cube] = list(cubes)
        for meta in META_CUBES:
            if meta.name not in existing:
                merged.append(meta)
                existing.add(meta.name)

        # Resolve ``extends`` chains — flatten inherited measures /
        # dimensions / time_dimensions / segments by name. Detect cycles.
        merged = _resolve_extends(merged)

        # Validate primary_key declarations — must name a real dimension.
        for c in merged:
            if c.primary_key is not None:
                dim_names = {d.name for d in c.dimensions}
                if c.primary_key not in dim_names:
                    raise ValueError(
                        f"Cube {c.name!r} declares primary_key="
                        f"{c.primary_key!r} but the cube has no dimension "
                        f"by that name. Declare it as a Dimension or pick "
                        f"a different primary_key."
                    )

        # Auto-derive Join edges from Dimension.foreign_key declarations.
        # An explicit Join with the same target wins — no duplicates.
        by_name: dict[str, Cube] = {c.name: c for c in merged}
        for cube in merged:
            inferred: list[Join] = []
            explicit_targets = {j.to for j in cube.joins}
            for dim in cube.dimensions:
                fk = dim.foreign_key
                if fk is None:
                    continue
                if fk not in by_name:
                    raise ValueError(
                        f"Cube {cube.name!r}, dimension {dim.name!r}: "
                        f"foreign_key={fk!r} names a cube not in the "
                        f"catalog. Known cubes: {sorted(by_name)}."
                    )
                target = by_name[fk]
                if target.primary_key is None:
                    raise ValueError(
                        f"Cube {cube.name!r}, dimension {dim.name!r}: "
                        f"foreign_key={fk!r} requires the target cube "
                        f"to declare a primary_key. Add primary_key="
                        f"'<dim>' to cube {fk!r}."
                    )
                if fk in explicit_targets:
                    continue  # explicit Join wins
                inferred.append(
                    Join(
                        to=fk,
                        relationship="many_to_one",
                        on=f"{{{cube.alias}}}.{dim.name} = {{{target.alias}}}.{target.primary_key}",
                    )
                )
            if inferred:
                # Replace the cube with a copy carrying the augmented joins.
                # Cube isn't frozen, but stay disciplined and use model_copy.
                merged[merged.index(cube)] = cube.model_copy(
                    update={"joins": [*cube.joins, *inferred]}
                )

        known = {c.name for c in merged}
        for c in merged:
            for j in c.joins:
                if j.to not in known:
                    raise ValueError(
                        f"Cube {c.name!r} declares Join(to={j.to!r}) but "
                        f"{j.to!r} is not in the catalog. "
                        f"Known cubes: {sorted(known)}."
                    )

        self._cubes: list[Cube] = merged
        self._by_name: dict[str, Cube] = {c.name: c for c in merged}

        # Validate views: every field target must resolve to a real
        # cube.field, and view names can't collide with cube names.
        view_list: list[View] = list(views or [])
        seen_view_names: set[str] = set()
        for v in view_list:
            if v.name in seen_view_names:
                raise ValueError(f"Catalog has duplicate view name {v.name!r}.")
            seen_view_names.add(v.name)
            if v.name in self._by_name:
                raise ValueError(
                    f"View {v.name!r} collides with cube name {v.name!r}. "
                    "View and cube names share a namespace; rename one."
                )
            for local, target_ref in v.fields.items():
                cube_name, field_name = target_ref.split(".", 1)
                if cube_name not in self._by_name:
                    raise ValueError(
                        f"View {v.name!r}, field {local!r}: target "
                        f"cube {cube_name!r} not in the catalog. "
                        f"Known cubes: {sorted(self._by_name)}."
                    )
                target_cube = self._by_name[cube_name]
                cube_field_names = {f.name for f in target_cube.measures}
                cube_field_names |= {f.name for f in target_cube.dimensions}
                cube_field_names |= {f.name for f in target_cube.time_dimensions}
                if field_name not in cube_field_names:
                    raise ValueError(
                        f"View {v.name!r}, field {local!r}: "
                        f"{cube_name}.{field_name} is not a known measure "
                        f"or dimension on cube {cube_name!r}."
                    )
        self.views: dict[str, View] = {v.name: v for v in view_list}
        self._policy: PolicyFn | None = policy

    @property
    def policy(self) -> PolicyFn | None:
        """The optional custom-visibility predicate registered at
        construction time. ``None`` means cube visibility is governed
        purely by ``Cube.required_roles``."""
        return self._policy

    def as_dict(self) -> dict[str, Cube]:
        """Return ``{cube.name: Cube}`` — the shape ``compile_query`` consumes."""
        return dict(self._by_name)

    def compile(
        self,
        query: SemanticQuery,
        *,
        context: dict[str, str] | None = None,
        viewer: AuthContext | None = None,
    ) -> Compiled:
        """Compile a ``SemanticQuery`` against this catalog. Thin wrapper
        around ``semql.compile.compile_query``.

        When ``viewer`` is provided, the compiler:
        - Refuses queries that touch a cube the viewer cannot see
          (``Cube.required_roles`` ANY-match + optional ``policy``).
        - Auto-binds ``ctx.viewer_id`` from ``viewer.viewer_id`` so
          ``security_sql`` fragments referencing it get a parameter
          (never a SQL literal).
        """
        from semql.compile import compile_query

        return compile_query(
            query,
            self._by_name,
            context=context,
            views=self.views,
            viewer=viewer,
            policy=self._policy,
        )

    def prompt(
        self,
        *,
        only_exposed: bool = True,
        include_introspection: bool = False,
        viewer: AuthContext | None = None,
    ) -> str:
        """Render the planner prompt fragment for this catalog. Thin
        wrapper around ``semql.prompt.build_planner_prompt_fragment``.

        When ``viewer`` is provided, the catalogue block shrinks to the
        cubes the viewer is allowed to see."""
        from semql.prompt import build_planner_prompt_fragment

        return build_planner_prompt_fragment(
            self._by_name,
            only_exposed=only_exposed,
            include_introspection=include_introspection,
            views=self.views,
            viewer=viewer,
            policy=self._policy,
        )

    def __iter__(self) -> Iterator[Cube]:
        return iter(self._cubes)

    def __len__(self) -> int:
        return len(self._cubes)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name


def _resolve_extends(cubes: list[Cube]) -> list[Cube]:
    """Flatten ``Cube.extends`` chains into self-contained cubes.

    Each child cube inherits the parent's measures / dimensions /
    time_dimensions / segments by name. Child overrides win;
    new items append. Other settings stay on the child.

    Detects cycles and unknown parents."""
    by_name = {c.name: c for c in cubes}

    def _flatten(name: str, stack: tuple[str, ...]) -> Cube:
        cube = by_name[name]
        if cube.extends is None:
            return cube
        if cube.extends == name or cube.extends in stack:
            chain = " -> ".join((*stack, name, cube.extends))
            raise ValueError(f"Cube {name!r}: extends cycle detected ({chain}).")
        if cube.extends not in by_name:
            raise ValueError(
                f"Cube {name!r}: extends={cube.extends!r} names a cube "
                f"not in the catalog. Known cubes: {sorted(by_name)}."
            )
        parent = _flatten(cube.extends, (*stack, name))

        def _merge_by_name(parent_list: list[_T], child_list: list[_T]) -> list[_T]:
            by_field_name: dict[str, _T] = {f.name: f for f in parent_list}
            for f in child_list:
                by_field_name[f.name] = f
            return list(by_field_name.values())

        return cube.model_copy(
            update={
                "measures": _merge_by_name(parent.measures, cube.measures),
                "dimensions": _merge_by_name(parent.dimensions, cube.dimensions),
                "time_dimensions": _merge_by_name(parent.time_dimensions, cube.time_dimensions),
                "segments": _merge_by_name(parent.segments, cube.segments),
            }
        )

    resolved: list[Cube] = []
    for c in cubes:
        if c.extends is None:
            resolved.append(c)
        else:
            resolved.append(_flatten(c.name, ()))
    return resolved


__all__ = ["Catalog"]
