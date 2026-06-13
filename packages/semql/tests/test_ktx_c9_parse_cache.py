"""C9 (ktx-ports M1) — cache parsed catalog SQL fragments.

The same measure/dimension ``expr`` string is parsed many times across a
compilation. ``_parse_fragment`` memoises on ``(sql, dialect)``. Because
callers reparent the returned AST into a larger expression (and the C7
reserved-word pass mutates it in place), the cache MUST hand out an
independent copy each call — a shared node would be corrupted across uses.
"""

from __future__ import annotations

from semql.compile import _parse_fragment, _parse_fragment_cached


def test_parse_fragment_is_cached() -> None:
    _parse_fragment_cached.cache_clear()
    _parse_fragment("t.amount + 1", "postgres")
    before = _parse_fragment_cached.cache_info()
    _parse_fragment("t.amount + 1", "postgres")
    after = _parse_fragment_cached.cache_info()
    assert after.hits == before.hits + 1, (before, after)


def test_parse_fragment_returns_independent_copies() -> None:
    a = _parse_fragment("t.amount", "postgres")
    b = _parse_fragment("t.amount", "postgres")
    # Distinct objects, so reparenting/mutating one cannot corrupt the other.
    assert a is not b
    a.set("alias", "x")  # mutate one
    assert b.args.get("alias") is None, "cached fragment leaked a mutation"
