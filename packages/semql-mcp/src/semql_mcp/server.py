# pyright: reportUnusedFunction=false
# FastMCP's ``@mcp.tool`` decorators register the wrapped function with
# the server; pyright sees the local name as "unused" because it can't
# follow the decorator's side effect.
"""MCP server wrapping a ``semql.Catalog``.

The server exposes the compiler / validator / prompt-renderer surfaces
as MCP tools so an LLM (or any MCP client) can plan and reason about
semantic queries against the catalogue.

By default the server is **compile-only**: ``semql`` is pure, so is
this server, and callers run the emitted SQL against whatever backend
they own. Pass an ``executor`` at construction to opt into row-returning
mode — a ``query_execute`` tool registers in addition to the
compile-only tools, runs the SQL against the executor, and returns
both the SQL and the rows. The executor is the only stateful surface
the server owns; everything else stays pure.

Tools always registered:
- ``query_semantic(spec, context?)`` — compile a SemanticQuery, return
  ``{sql, params, columns, backend}``.
- ``validate(spec)`` — collect-all static validation; returns a list
  of ``ValidationError`` records.
- ``explain(spec, context?)`` — same as ``query_semantic`` but returns
  just the SQL string.
- ``catalog_prompt(only_exposed=True, include_introspection=False)`` —
  planner prompt fragment.

Registered only when ``executor`` is supplied:
- ``query_execute(spec, context?)`` — compile + run; returns the
  ``query_semantic`` shape plus ``rows: list[dict]``.

Transports: stdio (FastMCP default) plus anything FastMCP supports
out of the box. Use ``server.run(transport="stdio")`` for a
CLI-launched process, or pass ``server.mcp`` to a custom transport.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Literal

from fastmcp import FastMCP
from semql import Catalog
from semql.spec import SemanticQuery
from semql.validate import ValidationError
from semql.validate import validate as validate_query

Transport = Literal["stdio", "http", "sse", "streamable-http"]
"""FastMCP transport identifiers."""

Executor = Callable[[str, dict[str, Any]], list[dict[str, Any]]]
"""``(sql, params) -> rows`` — sync executor surface.

Callers provide their own database driver (psycopg, clickhouse-connect,
DuckDB, ...) and adapt its row shape to a list of dicts. The MCP
server never imports a database driver itself."""


class MCPServer:
    """An MCP server exposing a SemQL ``Catalog`` to MCP clients.

    The ``mcp`` attribute is the underlying ``FastMCP`` instance — pass
    it to a ``fastmcp.Client`` for in-process testing, or call
    ``server.run(transport=...)`` to launch a real transport."""

    def __init__(
        self,
        catalog: Catalog,
        *,
        executor: Executor | None = None,
        name: str = "semql",
    ) -> None:
        self.catalog = catalog
        self.executor = executor
        self.mcp = FastMCP(name=name)
        self._register_tools()

    def _register_tools(self) -> None:
        catalog = self.catalog
        executor = self.executor

        @self.mcp.tool(
            name="query_semantic",
            description=(
                "Compile a SemanticQuery against the catalogue and return "
                "the emitted SQL, the bound parameters, and the output "
                "column names. Pass ``context`` for ``{schema}`` / "
                "``{ctx.X}`` substitution at compile time."
            ),
        )
        def query_semantic(
            spec: SemanticQuery,
            context: dict[str, str] | None = None,
        ) -> dict[str, Any]:
            try:
                compiled = catalog.compile(spec, context=context)
            except Exception as exc:
                return _error_payload(exc)
            return {
                "backend": compiled.backend.value,
                "sql": compiled.sql,
                "params": compiled.params,
                "columns": compiled.columns,
            }

        @self.mcp.tool(
            name="validate",
            description=(
                "Run collect-all static validation. Returns a list of "
                "structured ValidationError records — empty when the "
                "query would compile cleanly."
            ),
        )
        def validate(spec: SemanticQuery) -> list[dict[str, Any]]:
            errors: list[ValidationError] = validate_query(spec, catalog)
            return [asdict(e) for e in errors]

        @self.mcp.tool(
            name="explain",
            description=(
                "Compile a SemanticQuery and return just the SQL string. "
                "Equivalent to ``query_semantic(...).sql`` — handy for "
                "debugging 'what would you have run' without the params "
                "envelope."
            ),
        )
        def explain(
            spec: SemanticQuery,
            context: dict[str, str] | None = None,
        ) -> str:
            try:
                compiled = catalog.compile(spec, context=context)
            except Exception as exc:
                return f"-- compile failed: {exc}"
            return compiled.sql

        @self.mcp.tool(
            name="catalog_prompt",
            description=(
                "Render the planner prompt fragment for this catalogue — "
                "what an LLM planner would see to learn the catalogue's "
                "vocabulary and the SemanticQuery contract."
            ),
        )
        def catalog_prompt(
            only_exposed: bool = True,
            include_introspection: bool = False,
        ) -> str:
            return catalog.prompt(
                only_exposed=only_exposed,
                include_introspection=include_introspection,
            )

        if executor is not None:

            @self.mcp.tool(
                name="query_execute",
                description=(
                    "Compile a SemanticQuery, execute it against the "
                    "configured database, and return both the SQL/params "
                    "envelope and the resulting rows. Available only when "
                    "the server was constructed with an ``executor``. "
                    "Errors from compile or execute surface as a "
                    "structured ``{error}`` payload."
                ),
            )
            def query_execute(
                spec: SemanticQuery,
                context: dict[str, str] | None = None,
            ) -> dict[str, Any]:
                try:
                    compiled = catalog.compile(spec, context=context)
                except Exception as exc:
                    return _error_payload(exc)
                try:
                    rows = executor(compiled.sql, compiled.params)
                except Exception as exc:
                    return _error_payload(exc) | {
                        "sql": compiled.sql,
                        "params": compiled.params,
                    }
                return {
                    "backend": compiled.backend.value,
                    "sql": compiled.sql,
                    "params": compiled.params,
                    "columns": compiled.columns,
                    "rows": rows,
                }

    def run(self, transport: Transport = "stdio", **kwargs: Any) -> None:  # noqa: ANN401
        """Launch the server on ``transport``.

        Defaults to stdio so a parent process can spawn the server and
        speak JSON-RPC over its stdin/stdout. Forwards remaining kwargs
        to FastMCP."""
        self.mcp.run(transport=transport, **kwargs)


def _error_payload(exc: Exception) -> dict[str, Any]:
    """Turn an exception into a structured tool response.

    The MCP client should be able to surface the failure mode to the
    planner; raising would just crash the tool call. ``code`` matches
    SemQL's error-leaf class names so callers can branch on them
    without parsing the message."""
    return {
        "error": {
            "code": type(exc).__name__,
            "message": str(exc),
        }
    }


__all__ = ["Executor", "MCPServer"]
