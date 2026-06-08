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
