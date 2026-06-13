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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal

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


def enrich_all(
    rows: list[dict[str, object]],
    catalog: Catalog,
    ctx: ResolutionContext,
) -> list[dict[str, object]]:
    """Apply every catalog lookup's enricher to ``rows`` in one call.

    For each :class:`~semql.model.Lookup` whose dimension column is present
    in the result, delegates to :func:`enrich_result` (which no-ops when the
    lookup has no enricher). Saves callers hand-rolling the per-lookup loop:

        rows = enrich_all(rows, catalog, ctx)

    The match is by the dimension's *field* name (``orders.region_id`` →
    column ``region_id``); a query that aliased the column to something else
    isn't matched (enrichment is best-effort, never raises)."""
    if not rows:
        return rows
    present = set(rows[0].keys())
    # ``Catalog.lookups`` is a ``{dimension: Lookup}`` map.
    for lk in catalog.lookups.values():
        col = lk.dimension.rsplit(".", 1)[-1]
        if col in present:
            rows = enrich_result(rows, col, lk, ctx)
    return rows


# ---------------------------------------------------------------------------
# Declarative SQL enricher — the common "SELECT … FROM <reference table>" case
# ---------------------------------------------------------------------------


class _SafeFormat(dict):  # type: ignore[type-arg]
    """``str.format_map`` helper that leaves unknown ``{placeholders}`` intact
    so a ``table`` template only substitutes the keys ``ctx.context`` carries."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True)
class _SqlEnricher:
    """A :class:`~semql.model.MultiFieldEnricher` (and plan-time loader) that
    reads its values/fields from a reference table via a caller-supplied
    ``execute``. Built by :func:`sql_enricher`; see it for the contract."""

    table: str
    key: str
    fields: tuple[str, ...]
    execute: Callable[[str, list[Any]], Sequence[Mapping[str, Any]]]
    label: str | None = None
    paramstyle: Literal["qmark", "format"] = "qmark"

    def _table_for(self, ctx: ResolutionContext | None) -> str:
        if ctx is not None and "{" in self.table:
            return self.table.format_map(_SafeFormat(ctx.context))
        return self.table

    def _label_col(self) -> str:
        return self.label or (self.fields[0] if self.fields else self.key)

    def __call__(self, ctx: ResolutionContext) -> dict[str, str]:
        # Plan-time values: ``{key: label}`` so the planner sees both the
        # canonical id and a human label. ``max_inline`` on the Lookup caps
        # how many reach the prompt.
        label = self._label_col()
        sql = f"SELECT {self.key}, {label} FROM {self._table_for(ctx)}"  # noqa: S608
        rows = self.execute(sql, [])
        return {str(r[self.key]): str(r[label]) for r in rows}

    def enrich_fields(self, ids: list[str], ctx: ResolutionContext) -> dict[str, dict[str, str]]:
        if not ids:
            return {}
        placeholder = "?" if self.paramstyle == "qmark" else "%s"
        in_clause = ", ".join([placeholder] * len(ids))
        cols = ", ".join((self.key, *self.fields))
        sql = (  # noqa: S608 — identifiers are catalog-author-controlled; ids bind as params
            f"SELECT {cols} FROM {self._table_for(ctx)} WHERE {self.key} IN ({in_clause})"
        )
        out: dict[str, dict[str, str]] = {}
        for r in self.execute(sql, list(ids)):
            out[str(r[self.key])] = {f: "" if r.get(f) is None else str(r[f]) for f in self.fields}
        return out


def sql_enricher(
    *,
    table: str,
    key: str,
    fields: Sequence[str],
    execute: Callable[[str, list[Any]], Sequence[Mapping[str, Any]]],
    label: str | None = None,
    paramstyle: Literal["qmark", "format"] = "qmark",
) -> _SqlEnricher:
    """Build a multi-field enricher for the common "reference table" case
    without hand-writing a class.

    The enricher both surfaces plan-time values and enriches result rows::

        Lookup(
            dimension="orders.region_id",
            loader=sql_enricher(
                table="{schema}.regions", key="id",
                fields=["name", "manager", "currency"],
                execute=db.execute,        # (sql, params) -> list[dict]
            ),
        )

    - ``table`` / ``key`` / ``fields`` name the reference table, its join
      column, and the columns to attach (as ``<dim>__<field>``). They are
      catalog-author-controlled identifiers, interpolated into SQL; the
      dimension *values* always bind as parameters, never as literals.
    - ``table`` may carry ``{placeholders}`` (e.g. ``{schema}``) filled from
      ``ctx.context`` per request (multi-tenant); unknown keys stay literal.
    - ``execute(sql, params)`` is your DB driver returning a list of row
      mappings — the only I/O, kept at the edge so the catalog stays sans-io.
    - ``label`` is the plan-time display column (defaults to the first
      field). ``paramstyle`` picks the placeholder: ``"qmark"`` (``?``, the
      default — sqlite/duckdb) or ``"format"`` (``%s`` — psycopg/mysql).
    """
    return _SqlEnricher(
        table=table,
        key=key,
        fields=tuple(fields),
        execute=execute,
        label=label,
        paramstyle=paramstyle,
    )


__all__ = ["enrich_all", "enrich_result", "materialize", "resolve", "sql_enricher"]
