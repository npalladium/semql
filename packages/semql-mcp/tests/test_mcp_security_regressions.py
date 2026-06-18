"""Regression tests for MCP security-audit findings.

- SEMQL-MCP-LOOKUP-VIEWER (#6): the lookup tools must gate a dimension by
  its owning cube's visibility, so a low-role viewer can't read a hidden
  dimension's value vocabulary.
- SEMQL-MCP-VALIDATE-VIEWER (#7): the ``validate`` tool must thread the
  viewer so it can't act as a hidden-catalog oracle (clean ``[]`` for a
  role-gated cube a low-role viewer can't actually compile).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from fastmcp import Client
from semql import Catalog, Cube, Dialect, Dimension, Lookup, Measure, SemanticQuery
from semql.model import AuthContext
from semql_mcp import MCPServer


def _run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _gated_catalog() -> Catalog:
    """A single role-gated cube with a lookup on one of its dimensions."""
    secret = Cube(
        name="secret",
        dialect=Dialect.POSTGRES,
        table="secret",
        alias="s",
        required_roles=["admin"],
        measures=[Measure(name="total", sql="{s}.amount", agg="sum")],
        dimensions=[Dimension(name="region", sql="{s}.region", type="string")],
    )
    return Catalog(
        [secret],
        lookups=[
            Lookup(dimension="secret.region", values=("east", "west"), labels={"east": "East"})
        ],
    )


def _low_role_server() -> MCPServer:
    # The provider is authoritative; a connecting client can't assert roles.
    return MCPServer(_gated_catalog(), viewer_provider=lambda: AuthContext(viewer_id="u", roles=[]))


# ---------------------------------------------------------------------------
# #6 — lookup tools gate hidden dimensions
# ---------------------------------------------------------------------------


def test_list_lookup_values_refuses_hidden_dimension() -> None:
    s = _low_role_server()

    async def call() -> Any:  # noqa: ANN401
        async with Client(s.mcp) as c:
            return await c.call_tool("list_lookup_values", {"dimension": "secret.region"})

    result = _run(call())
    data: Any = result.data
    # The hidden value 'east' never appears anywhere in the response (check
    # before any type-narrowing), and a structured error is returned.
    assert "east" not in str(data)
    assert result.is_error or (isinstance(data, dict) and "error" in data)


def test_resolve_lookup_refuses_hidden_dimension() -> None:
    s = _low_role_server()

    async def call() -> Any:  # noqa: ANN401
        async with Client(s.mcp) as c:
            return await c.call_tool(
                "resolve_lookup", {"dimension": "secret.region", "query": "east"}
            )

    result = _run(call())
    data: Any = result.data
    assert result.is_error or (isinstance(data, dict) and "error" in data)


def test_lookup_values_visible_to_authorized_viewer() -> None:
    """An admin viewer still gets the vocabulary — the gate is role-specific."""
    s = MCPServer(
        _gated_catalog(), viewer_provider=lambda: AuthContext(viewer_id="a", roles=["admin"])
    )

    async def call() -> Any:  # noqa: ANN401
        async with Client(s.mcp) as c:
            return await c.call_tool("list_lookup_values", {"dimension": "secret.region"})

    data: Any = _run(call()).data
    assert "east" in data["values"]


# ---------------------------------------------------------------------------
# #7 — validate is not a hidden-catalog oracle
# ---------------------------------------------------------------------------


def test_validate_does_not_leak_hidden_cube_to_low_role_viewer() -> None:
    s = _low_role_server()
    spec = SemanticQuery(measures=["secret.total"], dimensions=["secret.region"])

    async def call() -> Any:  # noqa: ANN401
        async with Client(s.mcp) as c:
            return await c.call_tool("validate", {"spec": spec.model_dump()})

    errors: Any = _run(call()).data
    # A low-role viewer must NOT get a clean validation ([]). The gated cube
    # is filtered from the viewer's catalog, so the refs fail to resolve.
    assert errors, "validate leaked a role-gated cube as valid to a low-role viewer"


def test_validate_clean_for_authorized_viewer() -> None:
    s = MCPServer(
        _gated_catalog(), viewer_provider=lambda: AuthContext(viewer_id="a", roles=["admin"])
    )
    spec = SemanticQuery(measures=["secret.total"], dimensions=["secret.region"])

    async def call() -> Any:  # noqa: ANN401
        async with Client(s.mcp) as c:
            return await c.call_tool("validate", {"spec": spec.model_dump()})

    errors: Any = _run(call()).data
    assert errors == []
