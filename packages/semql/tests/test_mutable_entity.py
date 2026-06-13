"""Model-layer tests for the entity write surface — ``MutableEntity``,
``MutableField``, ``Op``, ``CtxRef`` — and the read-surface additions to
``Entity`` (``list_filters``, ``default_order``).

These cover construction/format validation only. Catalog-wiring
validation (refs resolve against real cubes/dims) lives in
``test_entities_catalog.py``.
"""

from __future__ import annotations

import pytest
from semql import CtxRef, Entity, MutableEntity, MutableField, Op

# ---------------------------------------------------------------------------
# Entity read-surface additions: list_filters, default_order
# ---------------------------------------------------------------------------


def test_entity_accepts_list_filters_and_default_order() -> None:
    e = Entity(
        name="order",
        cubes=["orders"],
        key="orders.id",
        list_filters=["orders.region", "orders.status"],
        default_order="orders.created_at desc",
    )
    assert e.list_filters == ["orders.region", "orders.status"]
    assert e.default_order == "orders.created_at desc"


def test_entity_list_filters_default_empty() -> None:
    e = Entity(name="order", cubes=["orders"])
    assert e.list_filters == []
    assert e.default_order is None


def test_entity_rejects_unqualified_list_filter() -> None:
    with pytest.raises(ValueError, match=r"(?i)list_filter|qualified|cube\.dim"):
        Entity(name="bad", cubes=["orders"], list_filters=["region"])


def test_entity_rejects_unqualified_default_order() -> None:
    with pytest.raises(ValueError, match=r"(?i)default_order|qualified|cube\.dim"):
        Entity(name="bad", cubes=["orders"], default_order="created_at desc")


def test_entity_default_order_accepts_bare_dim_without_direction() -> None:
    e = Entity(name="order", cubes=["orders"], default_order="orders.created_at")
    assert e.default_order == "orders.created_at"


def test_entity_default_order_rejects_bad_direction() -> None:
    with pytest.raises(ValueError, match=r"(?i)direction|asc|desc|default_order"):
        Entity(name="bad", cubes=["orders"], default_order="orders.created_at sideways")


# ---------------------------------------------------------------------------
# Op enum
# ---------------------------------------------------------------------------


def test_op_members() -> None:
    assert {o.value for o in Op} == {"insert", "update", "delete", "upsert"}


def test_op_is_str() -> None:
    """Op is a StrEnum so it round-trips through JSON and frozensets."""
    assert Op("insert") is Op.INSERT  # constructs from the wire string
    assert str(Op.INSERT) == "insert"  # renders as its value
    assert frozenset({Op.INSERT, Op.UPDATE}) == frozenset({"insert", "update"})


# ---------------------------------------------------------------------------
# MutableField
# ---------------------------------------------------------------------------


def test_mutable_field_defaults() -> None:
    f = MutableField(type="string")
    assert f.type == "string"
    assert f.required is False
    assert f.nullable is True
    assert f.immutable is False


def test_mutable_field_is_frozen() -> None:
    f = MutableField(type="number")
    with pytest.raises(Exception):  # noqa: B017, BLE001
        f.required = True


def test_mutable_field_rejects_bad_type() -> None:
    with pytest.raises(ValueError, match=r"(?i)type|validation"):
        MutableField(type="not_a_type")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CtxRef
# ---------------------------------------------------------------------------


def test_ctxref_carries_attr_name() -> None:
    r = CtxRef(attr="tenant")
    assert r.attr == "tenant"


def test_ctxref_rejects_empty_attr() -> None:
    with pytest.raises(ValueError, match=r"(?i)attr|empty"):
        CtxRef(attr="")


# ---------------------------------------------------------------------------
# MutableEntity
# ---------------------------------------------------------------------------


def _mutable() -> MutableEntity:
    return MutableEntity(
        name="order",
        cubes=["orders"],
        key="orders.id",
        target_cube="orders",
        operations=frozenset({Op.INSERT, Op.UPDATE}),
        mutable_fields={
            "amount": MutableField(type="number", required=True),
            "region": MutableField(type="string"),
        },
        pinned_values={"tenant_id": CtxRef(attr="tenant")},
    )


def test_mutable_entity_constructs() -> None:
    e = _mutable()
    assert e.target_cube == "orders"
    assert e.operations == frozenset({Op.INSERT, Op.UPDATE})
    assert set(e.mutable_fields) == {"amount", "region"}
    assert e.predicate_targeting is False
    assert e.kind == "entity"


def test_mutable_entity_is_an_entity() -> None:
    """isinstance gating is how the catalog decides a write is allowed."""
    assert isinstance(_mutable(), Entity)


def test_mutable_entity_requires_key() -> None:
    """PK targeting needs a key; refuse a key-less MutableEntity."""
    with pytest.raises(ValueError, match=r"(?i)key|MutableEntity"):
        MutableEntity(
            name="bad",
            cubes=["orders"],
            target_cube="orders",
            operations=frozenset({Op.UPDATE}),
            mutable_fields={"amount": MutableField(type="number")},
        )


def test_mutable_entity_target_cube_must_be_listed() -> None:
    with pytest.raises(ValueError, match=r"(?i)target_cube|cubes"):
        MutableEntity(
            name="bad",
            cubes=["orders"],
            key="orders.id",
            target_cube="payments",
            operations=frozenset({Op.UPDATE}),
            mutable_fields={"amount": MutableField(type="number")},
        )


def test_mutable_entity_pinned_column_cannot_also_be_mutable() -> None:
    """A pinned (ctx-derived) column is not LLM-supplied — it can't also
    appear in mutable_fields, or the two paths fight over the same column."""
    with pytest.raises(ValueError, match=r"(?i)pinned|mutable_fields"):
        MutableEntity(
            name="bad",
            cubes=["orders"],
            key="orders.id",
            target_cube="orders",
            operations=frozenset({Op.INSERT}),
            mutable_fields={"tenant_id": MutableField(type="string")},
            pinned_values={"tenant_id": CtxRef(attr="tenant")},
        )


def test_mutable_entity_rejects_empty_operations() -> None:
    with pytest.raises(ValueError, match=r"(?i)operation"):
        MutableEntity(
            name="bad",
            cubes=["orders"],
            key="orders.id",
            target_cube="orders",
            operations=frozenset(),
            mutable_fields={"amount": MutableField(type="number")},
        )


def test_mutable_entity_predicate_targeting_opt_in() -> None:
    e = MutableEntity(
        name="order",
        cubes=["orders"],
        key="orders.id",
        target_cube="orders",
        operations=frozenset({Op.DELETE}),
        mutable_fields={},
        predicate_targeting=True,
    )
    assert e.predicate_targeting is True
