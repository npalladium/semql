"""Extension hook Protocols for the semql compiler and prompt builder."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from semql.compile import CompiledQuery
    from semql.errors import CompileError
    from semql.model import AuthContext, Cube
    from semql.spec import SemanticQuery


@runtime_checkable
class CompileHook(Protocol):
    """Protocol for hooks that intercept the compile lifecycle."""

    def pre_compile(
        self,
        query: SemanticQuery,
        *,
        viewer: AuthContext | None = None,
        context: dict[str, str] | None = None,
    ) -> SemanticQuery | None: ...

    def post_compile(
        self,
        query: SemanticQuery,
        compiled: CompiledQuery,
        *,
        viewer: AuthContext | None = None,
        context: dict[str, str] | None = None,
    ) -> None: ...

    def on_compile_error(
        self,
        query: SemanticQuery,
        error: CompileError,
        *,
        viewer: AuthContext | None = None,
        context: dict[str, str] | None = None,
    ) -> None: ...


class BaseCompileHook:
    """Convenience base class with no-op implementations for CompileHook."""

    def pre_compile(
        self,
        query: SemanticQuery,
        *,
        viewer: AuthContext | None = None,
        context: dict[str, str] | None = None,
    ) -> SemanticQuery | None:
        return None

    def post_compile(
        self,
        query: SemanticQuery,
        compiled: CompiledQuery,
        *,
        viewer: AuthContext | None = None,
        context: dict[str, str] | None = None,
    ) -> None:
        pass

    def on_compile_error(
        self,
        query: SemanticQuery,
        error: CompileError,
        *,
        viewer: AuthContext | None = None,
        context: dict[str, str] | None = None,
    ) -> None:
        pass


@dataclass(frozen=True)
class AuditEvent:
    query: SemanticQuery = field(repr=False)
    outcome: str  # "ok" or "error"
    cubes_accessed: list[str]
    measures_accessed: list[str]
    filter_dimensions: list[str]
    error_code: str | None = None
    viewer_id: str | None = None


class AuditHook(BaseCompileHook):
    def __init__(self, sink: Callable[[AuditEvent], None]) -> None:
        self.sink = sink

    def _extract_cubes(self, query: SemanticQuery) -> list[str]:
        cubes: set[str] = set()
        for ref in query.measures + query.dimensions:
            if "." in ref:
                cubes.add(ref.split(".")[0])
        for f in query.filters:
            if "." in f.dimension:
                cubes.add(f.dimension.split(".")[0])
        return sorted(list(cubes))

    def _extract_measures(self, query: SemanticQuery) -> list[str]:
        measures: set[str] = set()
        for ref in query.measures:
            if "." in ref:
                measures.add(ref.split(".")[1])
            else:
                measures.add(ref)
        return sorted(list(measures))

    def _extract_filter_dims(self, query: SemanticQuery) -> list[str]:
        dims: set[str] = set()
        for f in query.filters:
            if "." in f.dimension:
                dims.add(f.dimension.split(".")[1])
            else:
                dims.add(f.dimension)
        return sorted(list(dims))

    def post_compile(
        self,
        query: SemanticQuery,
        compiled: CompiledQuery,
        *,
        viewer: AuthContext | None = None,
        context: dict[str, str] | None = None,
    ) -> None:
        viewer_id = viewer.viewer_id if viewer else None

        # When compiled, we can trust compiled.touched_cube_names
        # But we also have our manual extraction
        cubes = compiled.touched_cube_names

        event = AuditEvent(
            query=query,
            outcome="ok",
            cubes_accessed=cubes,
            measures_accessed=self._extract_measures(query),
            filter_dimensions=self._extract_filter_dims(query),
            viewer_id=viewer_id,
        )
        self.sink(event)

    def on_compile_error(
        self,
        query: SemanticQuery,
        error: CompileError,
        *,
        viewer: AuthContext | None = None,
        context: dict[str, str] | None = None,
    ) -> None:
        viewer_id = viewer.viewer_id if viewer else None

        event = AuditEvent(
            query=query,
            outcome="error",
            cubes_accessed=self._extract_cubes(query),
            measures_accessed=self._extract_measures(query),
            filter_dimensions=self._extract_filter_dims(query),
            error_code=getattr(error, "code", "CompileError"),
            viewer_id=viewer_id,
        )
        self.sink(event)


@runtime_checkable
class SqlRewriteHook(Protocol):
    """Protocol for hooks that rewrite the compiled SQL string."""

    def rewrite(
        self,
        compiled: CompiledQuery,
        *,
        query: SemanticQuery,
        viewer: AuthContext | None = None,
        context: dict[str, str] | None = None,
    ) -> CompiledQuery: ...


class QueryTagRewriter:
    def __init__(self, tags: dict[str, str]) -> None:
        self.tags = tags

    def rewrite(
        self,
        compiled: CompiledQuery,
        *,
        query: SemanticQuery,
        viewer: AuthContext | None = None,
        context: dict[str, str] | None = None,
    ) -> CompiledQuery:
        rendered_tags: list[str] = []
        for k, v in self.tags.items():
            if v == "{viewer_id}" and viewer is not None:
                rendered_tags.append(f"{k}={viewer.viewer_id}")
            else:
                rendered_tags.append(f"{k}={v}")

        tag_str = "/* " + " ".join(rendered_tags) + " */\n"
        from dataclasses import replace

        return replace(compiled, sql=tag_str + compiled.sql)


class LimitCapRewriter:
    def __init__(self, max_rows: int) -> None:
        self.max_rows = max_rows

    def rewrite(
        self,
        compiled: CompiledQuery,
        *,
        query: SemanticQuery,
        viewer: AuthContext | None = None,
        context: dict[str, str] | None = None,
    ) -> CompiledQuery:
        import sqlglot
        from sqlglot import exp
        from sqlglot.errors import ParseError

        from semql.dialect import dialect_for as sqlglot_dialect_for

        # We need to parse the SQL, modify the LIMIT, and emit it back.
        # This is a bit heavy, but it's the correct way.
        dialect = sqlglot_dialect_for(compiled.backend)
        try:
            ast = sqlglot.parse_one(compiled.sql, dialect=dialect)
        except ParseError:
            # If we can't parse it, leave it alone.
            return compiled

        if not isinstance(ast, exp.Select):
            return compiled

        current_limit = ast.args.get("limit")
        if current_limit is not None:
            # sqlglot limits are expressions, we need to try to parse it
            # For simplicity, if it's a number literal, we cap it.
            if isinstance(current_limit.expression, exp.Literal):
                try:
                    val = int(current_limit.expression.name)
                    if val > self.max_rows:
                        ast = ast.limit(self.max_rows)
                except ValueError:
                    pass
        else:
            ast = ast.limit(self.max_rows)

        new_sql = ast.sql(dialect=dialect, pretty=False, normalize_functions=False)
        from dataclasses import replace

        return replace(compiled, sql=new_sql)


@runtime_checkable
class CubePromptHook(Protocol):
    """Callable appended after a cube's block in the planner prompt.

    Return extra text (e.g. usage notes, example queries, warnings) to
    splice in after that cube's section. Return ``""`` to add nothing.
    """

    def __call__(self, cube: Cube) -> str: ...


@runtime_checkable
class ErrorTransformHook(Protocol):
    """Callable invoked when ``Catalog.compile()`` raises a ``CompileError``.

    Return a replacement exception to raise instead, or ``None`` to
    re-raise the original ``CompileError`` unchanged.
    """

    def __call__(self, error: CompileError) -> Exception | None: ...
