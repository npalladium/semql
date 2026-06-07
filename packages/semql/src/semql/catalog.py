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
from typing import TYPE_CHECKING

from semql.introspect import META_CUBES
from semql.model import Cube, Join

if TYPE_CHECKING:
    from semql.compile import Compiled
    from semql.spec import SemanticQuery


class Catalog:
    """A validated collection of cubes plus the convenience surface
    (``compile``, ``prompt``, ``as_dict``) downstream code wants."""

    def __init__(self, cubes: list[Cube]) -> None:
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

    def as_dict(self) -> dict[str, Cube]:
        """Return ``{cube.name: Cube}`` — the shape ``compile_query`` consumes."""
        return dict(self._by_name)

    def compile(
        self,
        query: SemanticQuery,
        *,
        context: dict[str, str] | None = None,
    ) -> Compiled:
        """Compile a ``SemanticQuery`` against this catalog. Thin wrapper
        around ``semql.compile.compile_query``."""
        from semql.compile import compile_query

        return compile_query(query, self._by_name, context=context)

    def prompt(
        self,
        *,
        only_exposed: bool = True,
        include_introspection: bool = False,
    ) -> str:
        """Render the planner prompt fragment for this catalog. Thin
        wrapper around ``semql.prompt.build_planner_prompt_fragment``."""
        from semql.prompt import build_planner_prompt_fragment

        return build_planner_prompt_fragment(
            self._by_name,
            only_exposed=only_exposed,
            include_introspection=include_introspection,
        )

    def __iter__(self) -> Iterator[Cube]:
        return iter(self._cubes)

    def __len__(self) -> int:
        return len(self._cubes)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name


__all__ = ["Catalog"]
