"""Shared identifier resolution for the semantic layer.

Both `compile.py` and `visualize.py` parse the planner's `cube.field`
references against the catalogue. Keeping a single resolver here makes
the validation, regex shape, and error class consistent across both
modules.
"""

from __future__ import annotations

import re

from semql.errors import ResolveError, UnknownIdentifierError, closest_match
from semql.model import Cube, Dimension, Measure, TimeDimension

_QUALIFIED_RE = re.compile(r"^([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)$", re.IGNORECASE)


def split(qualified: str) -> tuple[str, str]:
    m = _QUALIFIED_RE.match(qualified)
    if not m:
        raise ResolveError(f"Field reference must be 'cube.field', got: {qualified!r}")
    return m.group(1), m.group(2)


def resolve_field(
    qualified: str,
    catalog: dict[str, Cube],
) -> tuple[Cube, Measure | Dimension | TimeDimension]:
    cube_name, field_name = split(qualified)
    if cube_name not in catalog:
        hint = closest_match(cube_name, catalog.keys())
        known = ", ".join(sorted(catalog))
        suffix = f" Did you mean {hint!r}?" if hint else ""
        raise UnknownIdentifierError(
            f"Unknown cube: {cube_name!r}. Known cubes: {known}.{suffix}",
            kind="cube",
            name=cube_name,
            hint=hint,
        )
    cube = catalog[cube_name]
    for m in cube.measures:
        if m.name == field_name:
            return cube, m
    for d in cube.dimensions:
        if d.name == field_name:
            return cube, d
    for td in cube.time_dimensions:
        if td.name == field_name:
            return cube, td
    hint = closest_match(field_name, cube.field_names())
    known = ", ".join(sorted(cube.field_names()))
    suffix = f" Did you mean {hint!r}?" if hint else ""
    raise UnknownIdentifierError(
        f"Unknown field {field_name!r} on cube {cube_name!r}. Known fields: {known}.{suffix}",
        kind="field",
        name=field_name,
        cube=cube_name,
        hint=hint,
    )


__all__ = ["ResolveError", "UnknownIdentifierError", "split", "resolve_field"]
