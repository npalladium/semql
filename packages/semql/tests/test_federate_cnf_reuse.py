"""F1(a) — federate.py routes CNF through the core ``cnf.to_cnf`` engine.

federate.py used to carry its own hand-rolled CNF (``_negate_tree`` +
a naive distribute in ``_to_cnf``) that duplicated ``semql.cnf`` and,
unlike the core engine, never deduped. After unification the module
delegates to ``cnf.to_cnf`` and flattens the result into the
``(negated, Filter)`` clause-list the router consumes — so federation
inherits the core engine's dedup + idempotence for free.
"""

from __future__ import annotations

from semql.federate import _to_cnf  # pyright: ignore[reportPrivateUsage]
from semql.spec import BoolExpr, Filter


def _f(name: str) -> Filter:
    return Filter(dimension=f"o.{name}", op="eq", values=[1])


def test_to_cnf_produces_and_of_or_clauses() -> None:
    a, b, c = _f("a"), _f("b"), _f("c")
    # OR(a, AND(b, c)) → CNF (a OR b) AND (a OR c): two clauses, two literals each.
    tree = BoolExpr(op="or", children=[a, BoolExpr(op="and", children=[b, c])])
    cnf = _to_cnf(tree)
    assert len(cnf) == 2
    assert all(len(clause) == 2 for clause in cnf)


def test_to_cnf_dedups_redundant_literals_within_a_clause() -> None:
    """``OR(a, AND(a, b))`` distributes to ``(a OR a) AND (a OR b)``. The
    core engine reduces ``a OR a`` to ``a``; the old hand-rolled federate
    CNF left the duplicate literal in the clause."""
    a, b = _f("a"), _f("b")
    tree = BoolExpr(op="or", children=[a, BoolExpr(op="and", children=[a, b])])
    cnf = _to_cnf(tree)
    # No clause carries the same literal twice.
    for clause in cnf:
        keys = [(neg, f.dimension, f.op, tuple(f.values)) for neg, f in clause]
        assert len(keys) == len(set(keys)), f"duplicate literal in clause: {keys}"


def test_to_cnf_negated_filter_becomes_negated_literal() -> None:
    a = _f("a")
    cnf = _to_cnf(BoolExpr(op="not", children=[a]))
    assert cnf == [[(True, a)]]


def test_to_cnf_de_morgan_on_negated_or() -> None:
    """NOT(a OR b) → AND(NOT a, NOT b): two single-literal negated clauses."""
    a, b = _f("a"), _f("b")
    cnf = _to_cnf(BoolExpr(op="not", children=[BoolExpr(op="or", children=[a, b])]))
    assert cnf == [[(True, a)], [(True, b)]]


def test_to_cnf_single_filter_passthrough() -> None:
    a = _f("a")
    assert _to_cnf(a) == [[(False, a)]]
