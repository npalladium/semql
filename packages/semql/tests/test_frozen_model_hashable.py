"""Frozen catalog models must actually be hashable.

``model_config = ConfigDict(frozen=True)`` advertises hashability, but
Pydantic's generated ``__hash__`` raises ``unhashable type: 'list'`` /
``'dict'`` the moment a model carries a ``list`` or ``dict`` field — so
``Measure`` / ``Dimension`` / ``AuthContext`` / ``Join`` / … could not go
in a ``set`` or be a dict key despite claiming to be frozen.

A recursive value-based ``__hash__`` on a shared base restores the
contract while keeping Pydantic's field-wise ``__eq__`` — equal models
hash equal.
"""

from __future__ import annotations

from semql.model import (
    AuthContext,
    Dimension,
    Join,
    Measure,
    Rollup,
    ScopePredicate,
    Segment,
    TimeDimension,
)


def _instances() -> list[object]:
    return [
        Measure(name="rev", sql="{t}.x", agg="sum", unit="count", metadata={"k": "v"}),
        Dimension(name="region", sql="{t}.r", type="string", required_roles=["a"]),
        TimeDimension(name="ts", sql="{t}.ts"),
        Segment(name="active", sql="{t}.active = true"),
        Join(to="other", relationship="many_to_one", on="{t}.id = {o}.t_id"),
        AuthContext(viewer_id="u", roles=["analyst", "hr"], attrs={"team": "x"}),
        ScopePredicate(sql="{t}.a = {ctx.viewer_id}", ctx_keys=["viewer_id"]),
        Rollup(name="daily", physical_table="r.daily", dimensions=["region"], measures=["rev"]),
    ]


def test_frozen_models_with_collection_fields_are_hashable() -> None:
    for obj in _instances():
        # Must not raise — the whole point of the fix.
        hash(obj)
    # And usable in the containers frozen=True implies.
    as_set = set(_instances())
    assert len(as_set) == len(_instances())
    as_key = {obj: i for i, obj in enumerate(_instances())}
    assert len(as_key) == len(_instances())


def test_equal_frozen_models_hash_equal() -> None:
    a = Measure(name="rev", sql="{t}.x", agg="sum", unit="count", metadata={"k": "v"})
    b = Measure(name="rev", sql="{t}.x", agg="sum", unit="count", metadata={"k": "v"})
    assert a == b
    assert hash(a) == hash(b)
    ctx1 = AuthContext(viewer_id="u", roles=["a", "b"])
    ctx2 = AuthContext(viewer_id="u", roles=["a", "b"])
    assert ctx1 == ctx2
    assert hash(ctx1) == hash(ctx2)


def test_distinct_frozen_models_do_not_collapse_in_a_set() -> None:
    m1 = Measure(name="rev", sql="{t}.x", agg="sum", unit="count")
    m2 = Measure(name="rev", sql="{t}.x", agg="avg", unit="count")
    assert m1 != m2
    assert len({m1, m2}) == 2
