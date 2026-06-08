import pytest
from semql.compile import compile_query
from semql.logical import LogicalPlan, Scan, to_logical_plan
from semql.model import Backend, Cube, Dimension, Measure
from semql.spec import SemanticQuery


def test_to_logical_plan_simple() -> None:
    orders = Cube(
        name="orders",
        alias="orders",
        table="raw_orders",
        backend=Backend.POSTGRES,
        dimensions=[Dimension(name="id", sql="id", type="string")],
        measures=[Measure(name="revenue", sql="amount", agg="sum")],
    )
    catalog = {"orders": orders}
    query = SemanticQuery(
        measures=["orders.revenue"],
        dimensions=["orders.id"],
    )

    plan = to_logical_plan(query, catalog)
    assert isinstance(plan, LogicalPlan)
    assert len(plan.scans) == 1
    assert isinstance(plan.scans[0], Scan)
    assert plan.scans[0].cube.name == "orders"


def test_compile_query_does_not_depend_on_partial_logical_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orders = Cube(
        name="orders",
        alias="orders",
        table="raw_orders",
        backend=Backend.POSTGRES,
        dimensions=[Dimension(name="id", sql="id", type="string")],
        measures=[Measure(name="revenue", sql="amount", agg="sum")],
    )

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("compile_query must not call the partial LogicalPlan lowering")

    monkeypatch.setattr("semql.logical.to_logical_plan", fail_if_called)
    compiled = compile_query(SemanticQuery(measures=["orders.revenue"]), {"orders": orders})
    assert "SUM" in compiled.sql
