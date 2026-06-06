"""Shared identifier resolution for the semantic layer.

Both `compile.py` and `visualize.py` parse the planner's `cube.field`
references against the catalogue. Keeping a single resolver here makes
the validation, regex shape, and error class consistent across both
modules.
"""

from __future__ import annotations

import re

from semql.model import Cube, Dimension, Measure, TimeDimension

_QUALIFIED_RE = re.compile(r"^([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)$", re.IGNORECASE)


class ResolveError(Exception):
    """Raised for malformed or unknown identifiers. `CompileError`
    subclasses this so existing callers that catch `CompileError`
    continue to work; pure-visualisation callers catch `ResolveError`."""


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
        known = ", ".join(sorted(catalog))
        raise ResolveError(f"Unknown cube: {cube_name!r}. Known cubes: {known}.")
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
    known = ", ".join(sorted(cube.field_names()))
    raise ResolveError(
        f"Unknown field {field_name!r} on cube {cube_name!r}. Known fields: {known}."
    )


__all__ = ["ResolveError", "split", "resolve_field"]
