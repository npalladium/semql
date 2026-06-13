"""Row-returning MCP tools refuse non-read-only compiled SQL.

The compiler emits SELECT by construction, but RawSQL escape hatches can
splice author-controlled strings in. ``query_execute`` re-checks at the
execution choke point and returns a structured error instead of handing
the SQL to the executor.
"""

from __future__ import annotations

# Exercises module-private guard + a forced-bad CompiledQuery on purpose.
# pyright: reportPrivateUsage=false
import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest
from fastmcp import Client
from semql import Catalog, Cube, Dialect, Dimension, Measure, SemanticQuery
from semql.compile import ColumnMeta, CompiledQuery
from semql_mcp import MCPServer
from semql_mcp.server import ReadOnlyError, _guard_read_only


def _run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _compiled(sql: str, *, derived: list[str] | None = None) -> CompiledQuery:
    return CompiledQuery(
        dialect=Dialect.POSTGRES,
        sql=sql,
        params={},
        columns=["x"],
        column_meta=[ColumnMeta(name="x", kind="dimension", display_name="x")],
        derived_sources=derived or [],
    )


def test_guard_passes_clean_select() -> None:
    _guard_read_only(_compiled("SELECT x FROM t"))


def test_guard_rejects_non_select() -> None:
    with pytest.raises(ReadOnlyError):
        _guard_read_only(_compiled("DROP TABLE t"))


def test_guard_rejects_non_select_derived_source() -> None:
    with pytest.raises(ReadOnlyError):
        _guard_read_only(_compiled("SELECT x FROM t", derived=["DELETE FROM audit"]))


def _catalog() -> Catalog:
    cube = Cube(
        name="orders",
        dialect=Dialect.POSTGRES,
        table="orders",
        alias="o",
        measures=[Measure(name="revenue", sql="{o}.amount", agg="sum", unit="currency")],
        dimensions=[Dimension(name="region", sql="{o}.region", type="string")],
    )
    return Catalog([cube])


def test_query_execute_refuses_and_never_calls_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """If compile yields non-read-only SQL, query_execute returns a
    ReadOnlyError payload and the executor is never invoked."""
    calls: list[str] = []

    def executor(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append(sql)
        return []

    server = MCPServer(_catalog(), executor=executor, require_viewer=False)

    # Force the compiler to emit a non-SELECT (simulating a RawSQL hatch
    # that escaped the SELECT shape).
    def _bad_compile(*_a: object, **_k: object) -> CompiledQuery:
        return _compiled("DROP TABLE orders")

    monkeypatch.setattr(server.catalog, "compile", _bad_compile)

    async def call() -> dict[str, Any]:
        async with Client(server.mcp) as c:
            result = await c.call_tool(
                "query_execute",
                {"spec": SemanticQuery(measures=["orders.revenue"]).model_dump()},
            )
            return result.data  # type: ignore[no-any-return]

    out = _run(call())
    assert "error" in out
    assert out["error"]["code"] == "ReadOnlyError"
    assert calls == []
