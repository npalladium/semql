"""Tests for the conversational-rewrite vocabulary.

Five closed-enum ops — AddFilter / RemoveFilter / ChangeTimeWindow
/ ChangeGranularity / Drilldown — each a pure transformer over a
frozen :class:`SemanticQuery`. The transform must never mutate its
input; that's the contract the chat / drilldown UX relies on so
back-and-forth turns can keep prior states around for undo / branch.
"""

from __future__ import annotations

import pytest
from semql import (
    AddFilter,
    ChangeGranularity,
    ChangeTimeWindow,
    Drilldown,
    Filter,
    RemoveFilter,
    SemanticQuery,
    TimeWindow,
    rewrite,
)
from semql.errors import CompileError


def _base_query() -> SemanticQuery:
    return SemanticQuery(
        measures=["orders.revenue"],
        dimensions=["customers.region"],
        filters=[Filter(dimension="orders.status", op="eq", values=["paid"])],
        time_dimension=TimeWindow(
            dimension="orders.created_at",
            granularity="day",
            range=("2026-01-01", "2026-02-01"),
        ),
    )


# ---------------------------------------------------------------------------
# AddFilter
# ---------------------------------------------------------------------------


def test_add_filter_appends_to_filters() -> None:
    q = _base_query()
    op = AddFilter(filter=Filter(dimension="customers.tier", op="eq", values=["gold"]))
    out = rewrite(q, op)
    assert len(out.filters) == len(q.filters) + 1
    assert out.filters[-1].dimension == "customers.tier"
    # Input unchanged — frozen-spec invariant.
    assert len(q.filters) == 1


def test_add_filter_preserves_order() -> None:
    q = SemanticQuery(dimensions=["customers.region"])
    op_a = AddFilter(filter=Filter(dimension="orders.status", op="eq", values=["paid"]))
    op_b = AddFilter(filter=Filter(dimension="customers.tier", op="eq", values=["gold"]))
    out = rewrite(rewrite(q, op_a), op_b)
    assert [f.dimension for f in out.filters] == ["orders.status", "customers.tier"]


# ---------------------------------------------------------------------------
# RemoveFilter
# ---------------------------------------------------------------------------


def test_remove_filter_drops_matching_dim() -> None:
    q = _base_query()
    out = rewrite(q, RemoveFilter(dimension="orders.status"))
    assert all(f.dimension != "orders.status" for f in out.filters)


def test_remove_filter_drops_every_matching_filter() -> None:
    """Multiple Filters on the same dim (e.g. range bounds) all drop together."""
    q = SemanticQuery(
        measures=["orders.revenue"],
        filters=[
            Filter(dimension="orders.amount", op="gte", values=[100]),
            Filter(dimension="orders.amount", op="lt", values=[1000]),
            Filter(dimension="orders.status", op="eq", values=["paid"]),
        ],
    )
    out = rewrite(q, RemoveFilter(dimension="orders.amount"))
    assert len(out.filters) == 1
    assert out.filters[0].dimension == "orders.status"


def test_remove_filter_silent_noop_on_unknown_dim() -> None:
    """Chat UX is forgiving — removing a non-existent filter just returns
    the same query (the method may even return the identical object)."""
    q = _base_query()
    out = rewrite(q, RemoveFilter(dimension="orders.nonexistent"))
    assert out is q  # identity short-circuit when nothing matched


# ---------------------------------------------------------------------------
# ChangeTimeWindow
# ---------------------------------------------------------------------------


def test_change_time_window_replaces_range() -> None:
    q = _base_query()
    out = rewrite(q, ChangeTimeWindow(new_range=("2026-03-01", "2026-04-01")))
    assert out.time_dimension is not None
    assert out.time_dimension.range == ("2026-03-01", "2026-04-01")
    # Granularity preserved.
    assert out.time_dimension.granularity == "day"


def test_change_time_window_refuses_without_time_dimension() -> None:
    q = SemanticQuery(measures=["orders.revenue"], dimensions=["customers.region"])
    with pytest.raises(CompileError, match=r"(?i)requires the query"):
        rewrite(q, ChangeTimeWindow(new_range=("2026-01-01", "2026-02-01")))


# ---------------------------------------------------------------------------
# ChangeGranularity
# ---------------------------------------------------------------------------


def test_change_granularity_replaces_grain() -> None:
    q = _base_query()
    out = rewrite(q, ChangeGranularity(granularity="week"))
    assert out.time_dimension is not None
    assert out.time_dimension.granularity == "week"
    # Range preserved.
    assert out.time_dimension.range == ("2026-01-01", "2026-02-01")


def test_change_granularity_to_none_drops_bucket() -> None:
    q = _base_query()
    out = rewrite(q, ChangeGranularity(granularity=None))
    assert out.time_dimension is not None
    assert out.time_dimension.granularity is None


def test_change_granularity_refuses_without_time_dimension() -> None:
    q = SemanticQuery(measures=["orders.revenue"], dimensions=["customers.region"])
    with pytest.raises(CompileError, match=r"(?i)requires the query"):
        rewrite(q, ChangeGranularity(granularity="day"))


# ---------------------------------------------------------------------------
# Drilldown
# ---------------------------------------------------------------------------


def test_drilldown_appends_dimension() -> None:
    q = _base_query()
    out = rewrite(q, Drilldown(dimension="customers.tier"))
    assert out.dimensions == ["customers.region", "customers.tier"]


def test_drilldown_idempotent_for_existing_dim() -> None:
    """Drilling to a dim already in the query is a no-op so chat UX
    can call this without state tracking."""
    q = _base_query()
    out = rewrite(q, Drilldown(dimension="customers.region"))
    assert out is q


# ---------------------------------------------------------------------------
# Composition + invariants
# ---------------------------------------------------------------------------


def test_rewrite_does_not_mutate_input() -> None:
    """Frozen-spec invariant: ``rewrite(q, op)`` never alters ``q`` itself."""
    q = _base_query()
    snapshot = q.model_dump()
    for op in (
        AddFilter(filter=Filter(dimension="customers.tier", op="eq", values=["gold"])),
        RemoveFilter(dimension="orders.status"),
        ChangeTimeWindow(new_range=("2026-03-01", "2026-04-01")),
        ChangeGranularity(granularity="week"),
        Drilldown(dimension="customers.tier"),
    ):
        rewrite(q, op)
    assert q.model_dump() == snapshot


def test_method_form_matches_function_form() -> None:
    """``q.rewrite(op)`` is sugar over ``rewrite(q, op)`` — both
    produce the same result."""
    q = _base_query()
    op = Drilldown(dimension="customers.tier")
    assert q.rewrite(op).model_dump() == rewrite(q, op).model_dump()


def test_compose_multiple_rewrites_in_a_chat_turn() -> None:
    """A typical drill-down sequence: filter, bucket weekly, add a
    second dim. Each step a single op; the chain produces the final
    spec."""
    q = SemanticQuery(measures=["orders.revenue"])
    q = q.rewrite(AddFilter(filter=Filter(dimension="orders.status", op="eq", values=["paid"])))
    # Add a time dim by going through the SemanticQuery ctor (no
    # "AddTimeWindow" op in the vocabulary).
    q = q.model_copy(
        update={
            "time_dimension": TimeWindow(
                dimension="orders.created_at",
                granularity="day",
                range=("2026-01-01", "2026-02-01"),
            )
        }
    )
    q = q.rewrite(ChangeGranularity(granularity="week"))
    q = q.rewrite(Drilldown(dimension="customers.region"))
    assert q.dimensions == ["customers.region"]
    assert q.time_dimension is not None
    assert q.time_dimension.granularity == "week"
    assert {f.dimension for f in q.filters} == {"orders.status"}
