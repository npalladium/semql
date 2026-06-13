"""Dimension-value resolution helpers.

A :class:`semql.model.Lookup` declares the finite set of valid values
for a string dimension. ``Lookup`` lives in the model; this module
turns lookups into something callers can *use* at request time:

- :func:`materialize` — fire any ``loader`` against a
  :class:`ResolutionContext` and return the canonical
  ``(values, labels?)`` tuple, or ``None`` when the lookup is dynamic
  and no ``ctx`` was provided.
- :func:`resolve` — turn a free-text query ("paid east", "europe")
  into a list of canonical dimension values via exact / substring /
  fuzzy matching.

This module is the I/O surface of the lookup system. The compiler
never touches it; ``semql_prompt.planner_prompt(...)`` and any user-supplied
``resolve_<dim>`` tool do.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from semql.catalog import Catalog
from semql.model import Lookup, LookupEnricher, MultiFieldEnricher, ResolutionContext

# ---------------------------------------------------------------------------
# Materialization — turn a Lookup into a concrete (values, labels) tuple
# ---------------------------------------------------------------------------


def materialize(
    lookup: Lookup, ctx: ResolutionContext | None
) -> tuple[list[str], dict[str, str] | None] | None:
    """Materialize ``lookup`` to ``(values, labels?)`` for the given ``ctx``.

    Static lookups (``values=`` declared) return their inlined tuple
    regardless of ``ctx``. Dynamic lookups (``loader=`` declared) fire
    the loader against ``ctx`` — this is the I/O boundary. Returns
    ``None`` for dynamic lookups when ``ctx`` is ``None``, so callers
    can route to a "values resolved at runtime" fallback instead of
    inventing a stale answer."""
    if lookup.values is not None:
        return list(lookup.values), dict(lookup.labels) if lookup.labels else None
    if lookup.loader is None:
        return None  # validator forbids this; defensive
    if ctx is None:
        return None
    result = lookup.loader(ctx)
    if isinstance(result, dict):
        return list(result.keys()), dict(result)
    return list(result), dict(lookup.labels) if lookup.labels else None


# ---------------------------------------------------------------------------
# Resolution — free-text query → canonical dimension values
# ---------------------------------------------------------------------------


def resolve(
    catalog: Catalog,
    dimension: str,
    query: str,
    *,
    ctx: ResolutionContext | None = None,
    max_candidates: int = 5,
) -> list[str]:
    """Resolve a free-text ``query`` against the values of ``dimension``.

    ``dimension`` is the qualified ``cube.dim`` reference the Lookup
    was registered under. Returns canonical values (the lookup's *keys*,
    not labels) ranked best-match-first:

    1. Exact case-insensitive match against a canonical value or its
       label — returns a single-element list.
    2. Case-insensitive substring matches against canonical values and
       labels, preserving the lookup's declaration order.
    3. Fuzzy similarity fallback (``difflib.SequenceMatcher`` ratio
       against both canonical values and labels), up to
       ``max_candidates`` results.

    Returns an empty list when nothing matches — callers should treat
    that as "ask the user to clarify."

    Raises ``KeyError`` when ``dimension`` has no registered ``Lookup``.
    """
    if dimension not in catalog.lookups:
        raise KeyError(
            f"No Lookup registered for {dimension!r}. Registered: {sorted(catalog.lookups)}."
        )
    materialized = materialize(catalog.lookups[dimension], ctx)
    if materialized is None:
        # Dynamic lookup with no context — surface as empty rather
        # than guessing.
        return []
    values, labels = materialized
    if not values:
        return []

    needle = query.strip().lower()
    if not needle:
        return []

    label_for: dict[str, str] = labels or {}

    # Tier 1: exact case-insensitive match (against value or label).
    for v in values:
        if v.lower() == needle:
            return [v]
        if label_for.get(v, "").lower() == needle:
            return [v]

    # Tier 2: substring match (value or label).
    substring_hits = [
        v for v in values if needle in v.lower() or needle in label_for.get(v, "").lower()
    ]
    if substring_hits:
        return substring_hits[:max_candidates]

    # Tier 3: fuzzy similarity over (value or label) — rank by best ratio.
    scored: list[tuple[float, str]] = []
    for v in values:
        best = SequenceMatcher(None, needle, v.lower()).ratio()
        if v in label_for:
            best = max(best, SequenceMatcher(None, needle, label_for[v].lower()).ratio())
        scored.append((best, v))
    # Keep matches above a modest threshold so we don't return junk
    # candidates for completely unrelated queries.
    candidates = sorted(scored, key=lambda s: s[0], reverse=True)
    return [v for score, v in candidates if score >= 0.5][:max_candidates]


def enrich_result(
    rows: list[dict[str, object]],
    dim_name: str,
    lookup: Lookup,
    ctx: ResolutionContext,
) -> list[dict[str, object]]:
    """Attach reference fields to each row via the lookup's enricher.

    Two enricher shapes, checked in this order:

    - :class:`~semql.model.MultiFieldEnricher` (``enrich_fields``) attaches
      *several* columns per id — one ``<dim_name>__<field>`` column per
      resolved field (e.g. ``region_id__name``, ``region_id__manager``).
      An id absent from the mapping adds no columns for that row; a field
      absent for a present id is simply omitted.
    - :class:`~semql.model.LookupEnricher` (``enrich``) attaches a single
      ``<dim_name>__label`` column. Missing ids echo the raw id as the
      label.

    A loader implementing neither (or no loader at all) leaves rows
    unchanged. Rows whose dimension value is ``None`` are always skipped.
    When a loader implements both protocols the multi-field path wins.
    """
    loader = lookup.loader
    if loader is None:
        return rows
    ids = list({str(r[dim_name]) for r in rows if r.get(dim_name) is not None})
    if not ids:
        return rows

    if isinstance(loader, MultiFieldEnricher):
        field_map = loader.enrich_fields(ids, ctx)
        for row in rows:
            raw = row.get(dim_name)
            if raw is None:
                continue
            fields = field_map.get(str(raw))
            if not fields:
                continue
            for field_name, value in fields.items():
                row[f"{dim_name}__{field_name}"] = value
        return rows

    if isinstance(loader, LookupEnricher):
        mapping = loader.enrich(ids, ctx)
        label_col = f"{dim_name}__label"
        for row in rows:
            raw = row.get(dim_name)
            if raw is None:
                continue
            raw_str = str(raw)
            row[label_col] = mapping.get(raw_str, raw_str)
        return rows

    return rows


__all__ = ["enrich_result", "materialize", "resolve"]
