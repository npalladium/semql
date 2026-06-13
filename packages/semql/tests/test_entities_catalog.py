"""Catalog-wiring tests for entities (M1).

The model layer (``test_entity.py`` / ``test_mutable_entity.py``) only
checks an entity's *format*. The Catalog is where an entity's references
are checked against real cubes and dimensions — this file pins that
construction-time validation, for both the first-error ``Catalog(...)``
path and the collect-all ``CatalogSpec.from_iterables`` path.
"""

from __future__ import annotations

import pytest
from semql import (
    Catalog,
    Cube,
    Dimension,
    Entity,
    Measure,
    MutableEntity,
    MutableField,
    Op,
    TimeDimension,
)
from semql.catalog import CatalogSpec
from semql.model import Dialect


def _orders_cube() -> Cube:
    return Cube(
        name="orders",
        dialect=Dialect.POSTGRES,
        table="public.orders",
        alias="o",
        measures=[Measure(name="revenue", sql="{o}.amount", agg="sum", unit="currency")],
        dimensions=[
            Dimension(name="id", sql="{o}.id", type="number"),
            Dimension(name="region", sql="{o}.region", type="string"),
            Dimension(name="status", sql="{o}.status", type="string"),
        ],
        time_dimensions=[TimeDimension(name="created_at", sql="{o}.created_at")],
        primary_key="id",
    )


# ---------------------------------------------------------------------------
# Catalog.entities accessor + happy path
# ---------------------------------------------------------------------------


def test_catalog_accepts_entities() -> None:
    e = Entity(name="order", cubes=["orders"], key="orders.id")
    cat = Catalog([_orders_cube()], entities=[e])
    assert cat.entities["order"] is e


def test_catalog_entities_empty_by_default() -> None:
    cat = Catalog([_orders_cube()])
    assert cat.entities == {}


def test_catalog_accepts_full_read_entity() -> None:
    e = Entity(
        name="order",
        cubes=["orders"],
        key="orders.id",
        fields={"id": "orders.id", "amount": "orders.revenue"},
        list_filters=["orders.region", "orders.status"],
        default_order="orders.created_at desc",
    )
    cat = Catalog([_orders_cube()], entities=[e])
    assert cat.entities["order"].list_filters == ["orders.region", "orders.status"]


def test_vocabulary_only_entity_accepted() -> None:
    """key=None is valid in the catalog (prompt vocabulary only)."""
    e = Entity(name="order", cubes=["orders"])
    cat = Catalog([_orders_cube()], entities=[e])
    assert cat.entities["order"].key is None


# ---------------------------------------------------------------------------
# Construction-time ref validation (first-error path)
# ---------------------------------------------------------------------------


def test_entity_unknown_cube_refused() -> None:
    e = Entity(name="bad", cubes=["nope"], key="nope.id")
    with pytest.raises(ValueError, match=r"(?i)entity|cube|nope"):
        Catalog([_orders_cube()], entities=[e])


def test_entity_key_dim_must_exist() -> None:
    e = Entity(name="bad", cubes=["orders"], key="orders.ghost")
    with pytest.raises(ValueError, match=r"(?i)key|ghost|dimension"):
        Catalog([_orders_cube()], entities=[e])


def test_entity_field_target_must_resolve() -> None:
    e = Entity(name="bad", cubes=["orders"], fields={"x": "orders.ghost"})
    with pytest.raises(ValueError, match=r"(?i)ghost|field"):
        Catalog([_orders_cube()], entities=[e])


def test_entity_list_filter_dim_must_exist() -> None:
    e = Entity(name="bad", cubes=["orders"], list_filters=["orders.ghost"])
    with pytest.raises(ValueError, match=r"(?i)list_filter|ghost"):
        Catalog([_orders_cube()], entities=[e])


def test_entity_default_order_dim_must_exist() -> None:
    e = Entity(name="bad", cubes=["orders"], default_order="orders.ghost desc")
    with pytest.raises(ValueError, match=r"(?i)default_order|ghost"):
        Catalog([_orders_cube()], entities=[e])


def test_duplicate_entity_name_refused() -> None:
    a = Entity(name="order", cubes=["orders"])
    b = Entity(name="order", cubes=["orders"], key="orders.id")
    with pytest.raises(ValueError, match=r"(?i)duplicate|entity|order"):
        Catalog([_orders_cube()], entities=[a, b])


# ---------------------------------------------------------------------------
# MutableEntity catalog validation
# ---------------------------------------------------------------------------


def test_mutable_entity_field_must_exist_on_target_cube() -> None:
    e = MutableEntity(
        name="order",
        cubes=["orders"],
        key="orders.id",
        target_cube="orders",
        operations=frozenset({Op.UPDATE}),
        mutable_fields={"ghost": MutableField(type="string")},
    )
    with pytest.raises(ValueError, match=r"(?i)ghost|mutable|orders"):
        Catalog([_orders_cube()], entities=[e])


def test_mutable_entity_happy_path() -> None:
    e = MutableEntity(
        name="order",
        cubes=["orders"],
        key="orders.id",
        target_cube="orders",
        operations=frozenset({Op.UPDATE}),
        mutable_fields={"region": MutableField(type="string")},
    )
    cat = Catalog([_orders_cube()], entities=[e])
    assert isinstance(cat.entities["order"], MutableEntity)


# ---------------------------------------------------------------------------
# Catalog gates: allow_mutations + max_list_limit
# ---------------------------------------------------------------------------


def test_allow_mutations_defaults_false() -> None:
    assert Catalog([_orders_cube()]).allow_mutations is False


def test_allow_mutations_opt_in() -> None:
    assert Catalog([_orders_cube()], allow_mutations=True).allow_mutations is True


def test_max_list_limit_default() -> None:
    assert Catalog([_orders_cube()]).max_list_limit == 1000


def test_max_list_limit_override() -> None:
    assert Catalog([_orders_cube()], max_list_limit=200).max_list_limit == 200


# ---------------------------------------------------------------------------
# Collect-all path surfaces the same errors as structured codes
# ---------------------------------------------------------------------------


def test_from_iterables_collects_entity_errors() -> None:
    e = Entity(name="bad", cubes=["nope"], key="nope.id")
    _spec, errors = CatalogSpec.from_iterables(cubes=[_orders_cube()], entities=[e])
    codes = {err["code"] for err in errors}
    assert "unknown_entity_cube" in codes


def test_from_iterables_clean_entity_has_no_errors() -> None:
    e = Entity(name="order", cubes=["orders"], key="orders.id")
    spec, errors = CatalogSpec.from_iterables(cubes=[_orders_cube()], entities=[e])
    assert errors == []
    assert spec.entities == (e,)


def test_catalog_spec_roundtrips_entities() -> None:
    e = Entity(name="order", cubes=["orders"], key="orders.id")
    spec, _ = CatalogSpec.from_iterables(cubes=[_orders_cube()], entities=[e])
    restored = CatalogSpec.from_dict(spec.model_dump())
    assert restored.entities[0].name == "order"
