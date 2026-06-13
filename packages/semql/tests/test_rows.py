"""Tests for row-mode reads (M2): ``compile_fetch`` / ``compile_list``,
``RowPlan`` derivation, allowlist enforcement, keyset cursors, and the
custom-backend portability refusal.
"""

from __future__ import annotations

import pytest
from semql import (
    AuthContext,
    Catalog,
    Cube,
    Dimension,
    Entity,
    EntityFetch,
    EntityList,
    Measure,
    TimeDimension,
)
from semql.errors import CompileError
from semql.model import Dialect
from semql.rows import (
    compile_fetch,
    compile_list,
    decode_cursor,
    encode_cursor,
    non_portable_entities,
)


def _orders_cube(**kw: object) -> Cube:
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
        **kw,  # type: ignore[arg-type]
    )


def _catalog(entity: Entity, **cube_kw: object) -> Catalog:
    return Catalog([_orders_cube(**cube_kw)], entities=[entity])


def _order_entity(**kw: object) -> Entity:
    return Entity(
        name="order",
        cubes=["orders"],
        key="orders.id",
        list_filters=["orders.region", "orders.status", "orders.created_at"],
        default_order="orders.created_at desc",
        **kw,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# compile_fetch
# ---------------------------------------------------------------------------


def test_fetch_produces_sql_and_binds_key_as_param() -> None:
    cat = _catalog(_order_entity())
    out = compile_fetch(EntityFetch(entity="order", key=42), cat)
    assert out.sql is not None
    assert "SELECT" in out.sql.upper()
    assert "public.orders" in out.sql
    # bind-never-inline: the key value rides params, not the SQL text.
    assert 42 in out.params.values()
    assert "42" not in out.sql
    assert "LIMIT 1" in out.sql.upper().replace("  ", " ") or "LIMIT 1" in out.sql.upper()


def test_fetch_plan_is_derived() -> None:
    cat = _catalog(_order_entity())
    out = compile_fetch(EntityFetch(entity="order", key=42), cat)
    assert out.plan.source.cube == "orders"
    assert out.plan.source.backend == "postgres"
    assert out.plan.limit == 1
    assert [p.op for p in out.plan.predicates] == ["eq"]
    assert out.plan.predicates[0].column == "id"
    assert out.plan.params["key"] == 42


def test_fetch_field_subset() -> None:
    cat = _catalog(_order_entity())
    out = compile_fetch(EntityFetch(entity="order", key=1, fields=["id", "region"]), cat)
    assert out.plan.columns == ["id", "region"]


def test_fetch_unknown_field_refused() -> None:
    cat = _catalog(_order_entity())
    with pytest.raises(CompileError, match=r"(?i)unknown field|ghost"):
        compile_fetch(EntityFetch(entity="order", key=1, fields=["ghost"]), cat)


def test_fetch_unknown_entity_refused() -> None:
    cat = _catalog(_order_entity())
    with pytest.raises(CompileError, match=r"(?i)unknown entity"):
        compile_fetch(EntityFetch(entity="nope", key=1), cat)


def test_fetch_vocabulary_only_entity_refused() -> None:
    cat = _catalog(Entity(name="order", cubes=["orders"]))  # key=None
    with pytest.raises(CompileError, match=r"(?i)vocabulary-only|key"):
        compile_fetch(EntityFetch(entity="order", key=1), cat)


# ---------------------------------------------------------------------------
# compile_list — allowlist
# ---------------------------------------------------------------------------


def test_list_allowlisted_filter_ok() -> None:
    cat = _catalog(_order_entity())
    out = compile_list(EntityList(entity="order", where={"orders.region": "us"}), cat)
    assert out.sql is not None
    assert "us" in out.params.values()
    assert [p.column for p in out.plan.predicates] == ["region"]


def test_list_non_allowlisted_filter_refused() -> None:
    cat = _catalog(_order_entity())
    with pytest.raises(CompileError, match=r"(?i)allowlist|list_filters"):
        compile_list(EntityList(entity="order", where={"orders.id": 5}), cat)


def test_list_in_filter() -> None:
    cat = _catalog(_order_entity())
    out = compile_list(EntityList(entity="order", where={"orders.status": ["open", "closed"]}), cat)
    pred = next(p for p in out.plan.predicates if p.column == "status")
    assert pred.op == "in"
    assert out.plan.params[pred.params[0]] == ["open", "closed"]


def test_list_time_range_allowlisted() -> None:
    cat = _catalog(_order_entity())
    out = compile_list(
        EntityList(
            entity="order",
            time_range=("orders.created_at", "2026-01-01", "2026-02-01"),
        ),
        cat,
    )
    assert out.sql is not None
    pred = next(p for p in out.plan.predicates if p.op == "time_range")
    assert {out.plan.params[p] for p in pred.params} == {"2026-01-01", "2026-02-01"}


def test_list_time_range_not_allowlisted_refused() -> None:
    e = Entity(name="order", cubes=["orders"], key="orders.id", list_filters=["orders.region"])
    cat = _catalog(e)
    with pytest.raises(CompileError, match=r"(?i)time_range|list_filters"):
        compile_list(
            EntityList(entity="order", time_range=("orders.created_at", "a", "b")),
            cat,
        )


def test_list_limit_capped_by_catalog_policy() -> None:
    cat = Catalog([_orders_cube()], entities=[_order_entity()], max_list_limit=10)
    out = compile_list(EntityList(entity="order", limit=500), cat)
    assert out.plan.limit == 10


def test_list_default_order_applied_with_key_tiebreaker() -> None:
    cat = _catalog(_order_entity())
    out = compile_list(EntityList(entity="order"), cat)
    cols = [c for c, _ in out.plan.order]
    assert cols == ["created_at", "id"]  # default_order + key tiebreaker


# ---------------------------------------------------------------------------
# Keyset cursors (D9)
# ---------------------------------------------------------------------------


def test_cursor_roundtrip() -> None:
    token = encode_cursor(["2026-01-01", 7])
    assert decode_cursor(token) == ["2026-01-01", 7]


def test_list_cursor_decoded_into_where_on_sql_path() -> None:
    cat = _catalog(_order_entity())
    token = encode_cursor(["2026-01-01T00:00:00", 7])
    out = compile_list(EntityList(entity="order", cursor=token), cat)
    assert out.sql is not None
    # keyset values bound as params, never inlined
    assert "2026-01-01T00:00:00" in out.params.values()
    assert 7 in out.params.values()


def test_malformed_cursor_refused() -> None:
    cat = _catalog(_order_entity())
    with pytest.raises(CompileError, match=r"(?i)cursor"):
        compile_list(EntityList(entity="order", cursor="!!!notbase64!!!"), cat)


# ---------------------------------------------------------------------------
# Custom backend portability (§4)
# ---------------------------------------------------------------------------


def test_custom_backend_entity_no_sql_passthrough_cursor() -> None:
    e = _order_entity(custom_backend=True)
    cat = _catalog(e)
    out = compile_list(EntityList(entity="order", cursor="opaque-token"), cat)
    assert out.sql is None
    assert out.plan.cursor == "opaque-token"


def test_custom_backend_with_raw_security_sql_refused() -> None:
    e = _order_entity(custom_backend=True)
    cat = _catalog(e, security_sql="{o}.region = {ctx.viewer_id}")
    with pytest.raises(CompileError, match=r"(?i)custom|portab|raw|scope"):
        compile_fetch(EntityFetch(entity="order", key=1), cat)


def test_custom_backend_with_structured_tenancy_ok() -> None:
    """Discriminator tenancy is structured → portable; scope rides the plan."""
    e = _order_entity(custom_backend=True)
    cat = _catalog(
        e,
        tenancy="discriminator",
        tenancy_columns=["org_id"],
    )
    viewer = AuthContext(viewer_id="u1", tenant="acme")
    out = compile_fetch(EntityFetch(entity="order", key=1), cat, viewer=viewer)
    assert out.sql is None
    assert [p.column for p in out.plan.scope_predicates] == ["org_id"]
    assert "acme" in out.plan.params.values()


def test_non_portable_entities_lint() -> None:
    e = _order_entity(custom_backend=True)
    cat = _catalog(e, security_sql="{o}.region = 'x'")
    assert non_portable_entities(cat) == ["order"]


def test_sql_backend_with_security_sql_not_refused() -> None:
    """A normal SQL-backend entity may use raw security_sql freely."""
    e = _order_entity()  # custom_backend=False
    cat = _catalog(e, security_sql="{o}.region = 'x'")
    out = compile_fetch(EntityFetch(entity="order", key=1), cat)
    assert out.sql is not None


# ---------------------------------------------------------------------------
# Catalog convenience methods
# ---------------------------------------------------------------------------


def test_catalog_fetch_and_list_methods() -> None:
    cat = _catalog(_order_entity())
    assert cat.fetch(EntityFetch(entity="order", key=1)).sql is not None
    assert cat.list_rows(EntityList(entity="order")).sql is not None


@pytest.mark.parametrize("dialect", [Dialect.POSTGRES, Dialect.DUCKDB, Dialect.CLICKHOUSE])
def test_fetch_renders_per_dialect(dialect: Dialect) -> None:
    """The SQL path delegates to compile_query, so the entity read inherits
    each dialect's placeholder/identifier conventions."""
    cube = Cube(
        name="orders",
        dialect=dialect,
        table="public.orders",
        alias="o",
        measures=[Measure(name="revenue", sql="{o}.amount", agg="sum", unit="currency")],
        dimensions=[
            Dimension(name="id", sql="{o}.id", type="number"),
            Dimension(name="region", sql="{o}.region", type="string"),
        ],
        primary_key="id",
    )
    cat = Catalog([cube], entities=[Entity(name="order", cubes=["orders"], key="orders.id")])
    out = compile_fetch(EntityFetch(entity="order", key=7), cat)
    assert out.sql is not None and "public.orders" in out.sql
    assert 7 in out.params.values()
    assert "7" not in out.sql  # bound, never inlined
