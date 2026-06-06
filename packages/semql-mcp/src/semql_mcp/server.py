# pyright: reportUnusedFunction=false, reportUnknownParameterType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportMissingParameterType=false, reportUnknownMemberType=false
# - reportUnusedFunction: FastMCP's @mcp.tool decorators register the
#   wrapped function with the server; pyright sees the local name as
#   "unused" because it can't follow the decorator's side effect.
# - reportUnknownParameterType / Argument / Variable / Missing: the
#   per-cube tool factory builds dynamic ``Literal[...]`` annotations
#   at runtime and attaches them via ``__annotations__``; the def
#   itself intentionally has no static type hints.
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
from semql.model import Backend, Cube
from semql.spec import Filter, SemanticQuery, TimeWindow
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
        self._register_per_cube_tools()

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

    def _register_per_cube_tools(self) -> None:
        """For each exposed, non-META cube, register a ``query_<cube>``
        tool whose ``measures`` / ``dimensions`` / ``time_window.dimension``
        parameters are ``Literal``-typed enums of the cube's actual
        fields. Hidden cubes (``expose_in_prompt=False``) and META
        reflection cubes are skipped — multi-cube and introspection
        queries go through ``query_semantic``."""
        catalog = self.catalog
        executor = self.executor
        for cube in catalog.as_dict().values():
            if not cube.expose_in_prompt:
                continue
            if cube.backend is Backend.META:
                continue
            self.mcp.add_tool(_make_query_cube_tool(cube, catalog, executor))

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


def _make_query_cube_tool(
    cube: Cube,
    catalog: Catalog,
    executor: Executor | None,
) -> Callable[..., dict[str, Any]]:
    """Build a per-cube ``query_<cube>`` tool function.

    The returned function has ``__name__`` set to ``query_<cube_name>``,
    ``__doc__`` set to the cube's description, and ``__annotations__``
    set to typed signatures whose ``measures`` / ``dimensions`` /
    ``time_window.dimension`` are ``Literal``-typed enums of the cube's
    actual field names. FastMCP reads those via ``inspect.signature``
    to generate a JSON Schema with the enum constraint.

    The body auto-prefixes the bare field names with ``cube.name.`` so
    the planner doesn't have to repeat the cube name."""
    cube_name = cube.name
    measure_names = tuple(m.name for m in cube.measures)
    dimension_names = tuple(d.name for d in cube.dimensions)
    time_dim_names = tuple(td.name for td in cube.time_dimensions)
    field_names = (*measure_names, *dimension_names, *time_dim_names)

    # Build Literal types at runtime. ``Literal[("a", "b")]`` syntax is
    # supported in Python 3.11+ via the subscription protocol. The
    # types are attached to the function's ``__annotations__`` below —
    # the ``def`` itself can't reference them directly because this
    # module uses ``from __future__ import annotations`` (annotations
    # would be unresolvable string forms).
    measure_t = list[Literal[measure_names]] if measure_names else list[str]  # type: ignore[valid-type]
    dim_t = list[Literal[dimension_names]] if dimension_names else list[str]  # type: ignore[valid-type]
    field_t = Literal[field_names] if field_names else str
    order_t = list[tuple[field_t, Literal["asc", "desc"]]]  # type: ignore[valid-type]

    def query_cube_fn(  # type: ignore[no-untyped-def]  # noqa: ANN202 — signature attached via __annotations__ below
        measures=None,  # noqa: ANN001
        dimensions=None,  # noqa: ANN001
        filters=None,  # noqa: ANN001
        time_window=None,  # noqa: ANN001
        having=None,  # noqa: ANN001
        order=None,  # noqa: ANN001
        limit=None,  # noqa: ANN001
        offset=None,  # noqa: ANN001
        ungrouped=False,  # noqa: ANN001
        context=None,  # noqa: ANN001
    ):
        try:
            spec = SemanticQuery(
                measures=[f"{cube_name}.{m}" for m in (measures or [])],
                dimensions=[f"{cube_name}.{d}" for d in (dimensions or [])],
                filters=[
                    Filter(
                        dimension=_prefix(f.dimension, cube_name),
                        op=f.op,
                        values=f.values,
                    )
                    for f in (filters or [])
                ],
                time_dimension=_prefix_time_window(time_window, cube_name),
                having=[
                    Filter(dimension=h.dimension, op=h.op, values=h.values) for h in (having or [])
                ],
                order=[(o[0], o[1]) for o in (order or [])],
                limit=limit,
                offset=offset,
                ungrouped=ungrouped,
            )
        except Exception as exc:
            return _error_payload(exc)
        try:
            compiled = catalog.compile(spec, context=context)
        except Exception as exc:
            return _error_payload(exc)
        envelope: dict[str, Any] = {
            "backend": compiled.backend.value,
            "sql": compiled.sql,
            "params": compiled.params,
            "columns": compiled.columns,
        }
        if executor is None:
            return envelope
        try:
            envelope["rows"] = executor(compiled.sql, compiled.params)
        except Exception as exc:
            return _error_payload(exc) | envelope
        return envelope

    query_cube_fn.__name__ = f"query_{cube_name}"
    query_cube_fn.__doc__ = (cube.description or f"Query the {cube_name} cube.") + (
        f"\n\nMeasures: {', '.join(measure_names) or '(none)'}."
        f"\nDimensions: {', '.join(dimension_names) or '(none)'}."
        + (f"\nTime dimensions: {', '.join(time_dim_names)}." if time_dim_names else "")
        + "\n\nField names are bare (no cube prefix); the tool "
        "auto-qualifies them as it builds the SemanticQuery."
    )
    query_cube_fn.__annotations__ = {
        "measures": measure_t | None,
        "dimensions": dim_t | None,
        "filters": list[Filter] | None,
        "time_window": TimeWindow | None,
        "having": list[Filter] | None,
        "order": order_t | None,
        "limit": int | None,
        "offset": int | None,
        "ungrouped": bool,
        "context": dict[str, str] | None,
        "return": dict[str, Any],
    }
    return query_cube_fn


def _prefix(name: str, cube_name: str) -> str:
    """Auto-prefix a bare field name with the cube name.

    If the caller already qualified the name (``orders.region``), pass
    it through unchanged so cross-cube references in filters/having
    still work."""
    if "." in name:
        return name
    return f"{cube_name}.{name}"


def _prefix_time_window(tw: TimeWindow | None, cube_name: str) -> TimeWindow | None:
    if tw is None:
        return None
    return TimeWindow(
        dimension=_prefix(tw.dimension, cube_name),
        granularity=tw.granularity,
        range=tw.range,
    )


__all__ = ["Executor", "MCPServer"]
