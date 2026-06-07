"""Conversational rewrite ops over :class:`SemanticQuery`.

A chat / drill-down UX manipulates a running SemanticQuery one
intent at a time — "filter to last quarter", "break this down by
region", "drop the status filter", "switch to weekly". A closed-enum
vocabulary captures every supported manipulation so callers (LLM
agent, UI button bar, MCP client) can emit a single
:class:`RewriteOp` instead of mutating fields directly and risking
a malformed spec.

Each op is a frozen Pydantic model carrying just the data its
transform needs. :func:`rewrite` dispatches on the op type and
returns a new SemanticQuery (the input stays frozen and unchanged).

Vocabulary (closed; new ops are catalog-shape changes and need a
follow-up commit, not a runtime extension point):

- :class:`AddFilter` — append a :class:`Filter` to ``q.filters``.
- :class:`RemoveFilter` — drop every Filter on a given dimension.
- :class:`ChangeTimeWindow` — replace ``q.time_dimension.range``.
- :class:`ChangeGranularity` — replace ``q.time_dimension.granularity``.
- :class:`Drilldown` — add a dimension to ``q.dimensions`` (no-op if
  already present).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from semql.errors import CompileError
from semql.model import GranularityLiteral
from semql.spec import Filter, SemanticQuery, TimeWindow


class AddFilter(BaseModel):
    """Append ``filter`` to ``q.filters``.

    No deduplication — appending the same Filter twice produces a
    query with the predicate AND-composed with itself, which is a
    no-op semantically and a sign the caller should have run
    :class:`RemoveFilter` first."""

    model_config = ConfigDict(frozen=True)
    filter: Filter


class RemoveFilter(BaseModel):
    """Drop every ``Filter`` on the given qualified dimension.

    Silent no-op when no filter matches — chat UX should be
    forgiving about over-eager remove ops. Operates only on
    ``q.filters``; the ``q.where`` boolean tree isn't pruned because
    it's a single combinatorial expression and "remove the X leaf"
    isn't well-defined."""

    model_config = ConfigDict(frozen=True)
    dimension: str


class ChangeTimeWindow(BaseModel):
    """Replace ``q.time_dimension.range`` with ``new_range``.

    Refuses when the query has no ``time_dimension`` — the caller
    needs to add one first via a different surface. Granularity and
    other TimeWindow fields are preserved unchanged."""

    model_config = ConfigDict(frozen=True)
    new_range: tuple[str, str]


class ChangeGranularity(BaseModel):
    """Replace ``q.time_dimension.granularity``.

    Setting ``None`` removes the bucket (raw timestamps); setting
    a granularity literal switches the bucket. Refuses when the
    query has no ``time_dimension``."""

    model_config = ConfigDict(frozen=True)
    granularity: GranularityLiteral | None


class Drilldown(BaseModel):
    """Add ``dimension`` to ``q.dimensions``.

    No-op when the dimension is already present so a UI can call
    this idempotently. Doesn't try to remove ancestor dimensions on
    a drill path — the caller decides whether to bundle a
    :class:`RemoveFilter`-style op for the parent."""

    model_config = ConfigDict(frozen=True)
    dimension: str


RewriteOp = AddFilter | RemoveFilter | ChangeTimeWindow | ChangeGranularity | Drilldown


def rewrite(q: SemanticQuery, op: RewriteOp) -> SemanticQuery:
    """Apply a single :class:`RewriteOp` to ``q`` and return a new
    SemanticQuery — ``q`` itself is never mutated (it's frozen).

    Dispatches on the op type. Each branch uses ``model_copy(update=...)``
    on the frozen Pydantic model, so structural integrity (other
    fields stay valid) is guaranteed by Pydantic.
    """
    if isinstance(op, AddFilter):
        return q.model_copy(update={"filters": [*q.filters, op.filter]})
    if isinstance(op, RemoveFilter):
        kept = [f for f in q.filters if f.dimension != op.dimension]
        if len(kept) == len(q.filters):
            return q
        return q.model_copy(update={"filters": kept})
    if isinstance(op, ChangeTimeWindow):
        if q.time_dimension is None:
            raise CompileError(
                "ChangeTimeWindow requires the query to already declare "
                "a time_dimension. Set it first via the SemanticQuery "
                "constructor (this rewrite vocabulary is intentionally "
                "narrow — no add-time-window op)."
            )
        new_td = q.time_dimension.model_copy(update={"range": op.new_range})
        return q.model_copy(update={"time_dimension": new_td})
    if isinstance(op, ChangeGranularity):
        if q.time_dimension is None:
            raise CompileError(
                "ChangeGranularity requires the query to already declare a time_dimension."
            )
        new_td = q.time_dimension.model_copy(update={"granularity": op.granularity})
        return q.model_copy(update={"time_dimension": new_td})
    # Final branch — RewriteOp is a closed union; pyright proves the
    # last alternative reachable here. Static-only assertion keeps the
    # dispatch exhaustive if a new op is added without updating this
    # function (mypy / pyright will flag the missing branch).
    assert isinstance(op, Drilldown), op  # noqa: S101 — exhaustiveness gate
    if op.dimension in q.dimensions:
        return q
    return q.model_copy(update={"dimensions": [*q.dimensions, op.dimension]})


# ``SemanticQuery.rewrite(op)`` is declared in :mod:`semql.spec` so
# static type checkers see the method without a runtime monkey-patch.
# That stub does ``from semql.rewrite import rewrite`` at call time
# and delegates here.


# Re-export TimeWindow so callers building ops have the type handy.
__all__ = [
    "AddFilter",
    "ChangeGranularity",
    "ChangeTimeWindow",
    "Drilldown",
    "RemoveFilter",
    "RewriteOp",
    "TimeWindow",
    "rewrite",
]
