"""RawSQL marker on raw-string entry points.

The nine entry points that carry hand-written SQL are typed so the
trust boundary is explicit: the value is a :class:`RawSQL` instance at
runtime ("when raw SQL is used, SemQL says so"), while plain strings
still construct without change (backward compatible).
"""

from __future__ import annotations

from semql import (
    Cube,
    Dialect,
    Dimension,
    Join,
    Measure,
    RawSQL,
    Segment,
    TimeDimension,
)
from semql.model import DerivedTable, NamedCTE, ScopePredicate


def test_raw_sql_is_a_str_subclass() -> None:
    r = RawSQL("{o}.amount")
    assert isinstance(r, str)
    assert r == "{o}.amount"


def test_plain_string_coerced_to_raw_sql_on_field() -> None:
    d = Dimension(name="region", sql="{o}.region", type="string")
    assert isinstance(d.sql, RawSQL)
    assert d.sql == "{o}.region"


def test_all_raw_entry_points_are_marked() -> None:
    measure = Measure(name="paid", sql="{o}.amount", agg="sum", filter="{o}.status='paid'")
    assert isinstance(measure.sql, RawSQL)
    assert isinstance(measure.filter, RawSQL)

    masked = Dimension(
        name="ssn",
        sql="{o}.ssn",
        type="string",
        required_roles=["pii"],
        mask_roles=["pii"],
        mask_value="'***'",
    )
    assert isinstance(masked.mask_value, RawSQL)

    join = Join(to="customers", on="{o}.customer_id = {c}.id", relationship="many_to_one")
    assert isinstance(join.on, RawSQL)

    seg = Segment(name="paid", sql="{o}.status = 'paid'")
    assert isinstance(seg.sql, RawSQL)

    cte = NamedCTE(name="recent", sql="SELECT * FROM orders")
    assert isinstance(cte.sql, RawSQL)

    derived = DerivedTable(sql="SELECT 1 AS x")
    assert isinstance(derived.sql, RawSQL)

    scope = ScopePredicate(sql="{o}.tenant_id = {ctx.tenant}")
    assert isinstance(scope.sql, RawSQL)

    cube = Cube(
        name="orders",
        dialect=Dialect.POSTGRES,
        table="orders",
        alias="o",
        base_predicate="{o}.deleted_at IS NULL",
        security_sql="{o}.tenant_id = {ctx.tenant}",
        security_ctx_keys=["tenant"],
        dimensions=[Dimension(name="region", sql="{o}.region", type="string")],
    )
    assert isinstance(cube.base_predicate, RawSQL)
    assert isinstance(cube.security_sql, RawSQL)


def test_time_dimension_sql_is_marked() -> None:
    td = TimeDimension(name="placed_at", sql="{o}.placed_at")
    assert isinstance(td.sql, RawSQL)


def test_optional_raw_field_stays_none() -> None:
    m = Measure(name="count", sql="*", agg="count")
    assert m.filter is None


def test_round_trip_serialisation_preserves_value() -> None:
    d = Dimension(name="region", sql="{o}.region", type="string")
    reloaded = Dimension.model_validate(d.model_dump())
    assert reloaded.sql == "{o}.region"
    assert isinstance(reloaded.sql, RawSQL)
