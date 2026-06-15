# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# FastMCP exposes ``tool.meta`` as a plain dict, but the upstream
# type stub marks it ``Unknown`` so the ``.get(...)`` calls below
# would otherwise need per-call casts. The values are always
# primitive dict / list / str, so the looser check is safe here.
"""Tests for the visualisation MCP integration.

The visualizer lives in ``semql.visualize`` and is pure; this file
covers the *MCP Apps* wiring on top: the ``query_visualize`` tool,
the ``ui://semql/chart`` resource, the ``_stability: beta`` marker,
and the executor integration. The visualizer's own decision table
is tested in ``packages/semql/tests/test_visualize.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from fastmcp import Client
from semql import (
    Catalog,
    Cube,
    Dialect,
    Dimension,
    Measure,
    SemanticQuery,
)
from semql_mcp import CHART_RESOURCE_URI, MCPServer


def _run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _orders_catalog() -> Catalog:
    return Catalog(
        [
            Cube(
                name="orders",
                dialect=Dialect.POSTGRES,
                table="orders",
                alias="o",
                measures=[
                    Measure(name="revenue", sql="{o}.amount", agg="sum", unit="currency"),
                ],
                dimensions=[
                    Dimension(name="region", sql="{o}.region", type="string"),
                ],
            )
        ]
    )


def _client(server: MCPServer) -> Client[Any]:
    return Client(server.mcp)


# ---------------------------------------------------------------------------
# Registration — both the tool and the resource are always present
# ---------------------------------------------------------------------------


def test_query_visualize_tool_is_registered() -> None:
    s = MCPServer(_orders_catalog())

    async def fetch() -> set[str]:
        async with _client(s) as c:
            tools = await c.list_tools()
            return {t.name for t in tools}

    assert "query_visualize" in _run(fetch())


def test_chart_resource_is_registered() -> None:
    s = MCPServer(_orders_catalog())

    async def fetch() -> list[Any]:
        async with _client(s) as c:
            resources = await c.list_resources()
            return resources  # type: ignore[no-any-return]

    resources = _run(fetch())
    uris = {str(r.uri) for r in resources}
    assert CHART_RESOURCE_URI in uris


# ---------------------------------------------------------------------------
# BETA marker — surfaces in the tool description and the payload
# ---------------------------------------------------------------------------


def test_query_visualize_description_is_marked_beta() -> None:
    """The MCP Apps consumer reads the tool description to decide
    whether to surface the affordance; the BETA label has to be
    visible there, not just in the Python module's docstring."""
    s = MCPServer(_orders_catalog())

    async def fetch() -> str | None:
        async with _client(s) as c:
            tools = await c.list_tools()
            for t in tools:
                if t.name == "query_visualize":
                    return t.description  # type: ignore[no-any-return]
            return None

    desc = _run(fetch())
    assert desc is not None
    assert "BETA" in desc


def test_query_visualize_payload_is_marked_beta() -> None:
    """The ``_stability`` field on the JSON payload is the consumer's
    machine-readable signal that the shape may change. Bump it to
    'stable' when the iframe protocol and the rendered shape lock."""
    s = MCPServer(_orders_catalog())

    async def call() -> dict[str, Any]:
        async with _client(s) as c:
            result = await c.call_tool(
                "query_visualize",
                {
                    "spec": SemanticQuery(
                        measures=["orders.revenue"],
                        dimensions=["orders.region"],
                    ).model_dump(),
                    "n_rows": 3,
                },
            )
            return result.data  # type: ignore[no-any-return]

    out = _run(call())
    assert out.get("_stability") == "beta"


# ---------------------------------------------------------------------------
# MCP Apps metadata — tool points to the chart resource
# ---------------------------------------------------------------------------


def test_query_visualize_tool_is_app_visible_only() -> None:
    """``AppConfig(visibility=["app"])`` means the tool is only
    surfaced in MCP Apps-aware hosts; a plain chat client (which
    would treat ``model``-visible tools as part of its tool
    vocabulary) doesn't see the affordance. The ``_meta.ui``
    field is what the host reads to make this decision."""
    s = MCPServer(_orders_catalog())

    async def fetch() -> dict[str, Any] | None:
        async with _client(s) as c:
            tools = await c.list_tools()
            for t in tools:
                if t.name == "query_visualize":
                    return t.meta  # type: ignore[no-any-return]
            return None

    meta = _run(fetch())
    assert meta is not None
    # FastMCP returns the tool's ``meta`` (the ``AppConfig`` wire form)
    # as a plain dict. The ``ui`` sub-dict carries the MCP Apps
    # resource URI and visibility — the host reads these to decide
    # whether to surface the affordance.
    assert isinstance(meta, dict)
    ui = meta.get("ui")
    assert isinstance(ui, dict)
    assert ui.get("resourceUri") == CHART_RESOURCE_URI
    visibility = ui.get("visibility", [])
    assert "app" in visibility
    # The "model" channel is *not* opted in — the LLM-side of a
    # chat client doesn't get the visualisation tool.
    assert "model" not in visibility


# ---------------------------------------------------------------------------
# Tool behaviour — round-trips a compile + decision
# ---------------------------------------------------------------------------


def test_query_visualize_returns_decision_envelope() -> None:
    """Compile-only mode (no executor): the payload carries the
    ``VizDecision`` plus the SQL envelope so the caller can execute
    against its own backend and feed the rows to the iframe."""
    s = MCPServer(_orders_catalog())

    async def call() -> dict[str, Any]:
        async with _client(s) as c:
            result = await c.call_tool(
                "query_visualize",
                {
                    "spec": SemanticQuery(
                        measures=["orders.revenue"],
                        dimensions=["orders.region"],
                    ).model_dump(),
                    "n_rows": 3,
                },
            )
            return result.data  # type: ignore[no-any-return]

    out = _run(call())
    assert out["chart_type"] == "pie_chart"
    assert out["x_axis"] == "Region"
    assert out["y_axes"] == ["Revenue"]
    # SQL envelope for the caller to execute.
    assert "SUM" in out["sql"].upper()
    # No rows in compile-only mode.
    assert "rows" not in out
    # Reason is the structured value, not a debug string.
    assert out["reason"]["kind"] == "pie_small"


def test_query_visualize_runs_executor_when_provided() -> None:
    """When the server has an executor, the tool runs the SQL after
    compile and attaches the row data to the iframe payload, so the
    chart is actually drawable. The SQL/params envelope is still
    included so the host can re-execute or inspect."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_exec(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        return [
            {"region": "us", "revenue": 1000},
            {"region": "eu", "revenue": 700},
        ]

    s = MCPServer(_orders_catalog(), executor=fake_exec)

    async def call() -> dict[str, Any]:
        async with _client(s) as c:
            result = await c.call_tool(
                "query_visualize",
                {
                    "spec": SemanticQuery(
                        measures=["orders.revenue"],
                        dimensions=["orders.region"],
                    ).model_dump(),
                    "n_rows": 2,
                },
            )
            return result.data  # type: ignore[no-any-return]

    out = _run(call())
    assert len(calls) == 1
    assert "SUM" in calls[0][0].upper()
    assert out["chart_type"] == "pie_chart"
    assert out["rows"] == [
        {"region": "us", "revenue": 1000},
        {"region": "eu", "revenue": 700},
    ]


def test_query_visualize_shape_stats_overrides_pie() -> None:
    """The shape-stats override hook flows through the tool:
    ``has_negatives=True`` downgrades the natural pie to a bar and
    records the rejected pick in ``reason.alternatives``."""
    s = MCPServer(_orders_catalog())

    async def call() -> dict[str, Any]:
        async with _client(s) as c:
            result = await c.call_tool(
                "query_visualize",
                {
                    "spec": SemanticQuery(
                        measures=["orders.revenue"],
                        dimensions=["orders.region"],
                    ).model_dump(),
                    "n_rows": 3,
                    "shape_stats": {"has_negatives": True},
                },
            )
            return result.data  # type: ignore[no-any-return]

    out = _run(call())
    assert out["chart_type"] == "bar_chart"
    assert out["reason"]["kind"] == "shape_stats_fallback"
    assert "pie_chart" in out["reason"]["alternatives"]


def test_query_visualize_supported_charts_falls_back() -> None:
    """``supported_charts`` flows through: a client that can only draw
    a table refuses the natural pie and falls back to ``data_table``."""
    s = MCPServer(_orders_catalog())

    async def call() -> dict[str, Any]:
        async with _client(s) as c:
            result = await c.call_tool(
                "query_visualize",
                {
                    "spec": SemanticQuery(
                        measures=["orders.revenue"],
                        dimensions=["orders.region"],
                    ).model_dump(),
                    "n_rows": 3,
                    "supported_charts": ["bar_chart", "data_table"],
                },
            )
            return result.data  # type: ignore[no-any-return]

    out = _run(call())
    assert out["chart_type"] == "data_table"
    assert out["reason"]["kind"] == "client_capability_fallback"
    assert "pie_chart" in out["reason"]["alternatives"]


def test_query_visualize_handles_compile_error() -> None:
    """A bad spec returns a structured error payload rather than
    crashing the tool call — same contract as ``query_execute``."""
    s = MCPServer(_orders_catalog())

    async def call() -> dict[str, Any]:
        async with _client(s) as c:
            result = await c.call_tool(
                "query_visualize",
                {
                    "spec": {"measures": ["orders.nonexistent"], "dimensions": []},
                },
            )
            return result.data  # type: ignore[no-any-return]

    out = _run(call())
    assert "error" in out
    assert out["_stability"] == "beta"


# ---------------------------------------------------------------------------
# Resource — the iframe HTML
# ---------------------------------------------------------------------------


def test_chart_resource_serves_html() -> None:
    """The resource is a self-contained HTML page the host loads in
    an iframe. The exact markup is small and stable enough to assert
    on structurally — the iframe is sandboxed, so the CSP is strict
    and the page must not import anything from a CDN."""
    s = MCPServer(_orders_catalog())

    async def fetch() -> str:
        async with _client(s) as c:
            result = await c.read_resource(CHART_RESOURCE_URI)
            return result[0].text  # type: ignore[no-any-return]

    html = _run(fetch())
    # Structural shape — actual content evolves with the renderer.
    assert "<html" in html.lower()
    assert "<svg" in html.lower() or "svg" in html.lower()  # bar/line/pie path
    assert "BETA" in html
    # Self-contained: no external imports.
    assert "<script src" not in html
    assert "<link" not in html
