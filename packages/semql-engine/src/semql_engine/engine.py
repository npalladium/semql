"""In-process executor for :class:`semql.FederatedPlan`.

The :class:`Engine` runs each per-backend fragment via a registered
:class:`Adapter`, materialises the resulting rows into in-memory DuckDB
under the tables ``frag_0``, ``frag_1``, … expected by the plan's
``merge.sql``, and finally executes the merge to produce the final
shape.

Single-fragment plans (returned by :func:`semql.compile_federated_query`
when the query touches one backend) are handled identically — the merge
SQL is a trivial ``SELECT * FROM frag_0`` in that case.

The engine keeps a private DuckDB connection. Adapters that are
themselves DuckDB-backed run against their own connections; results
still flow through the engine's connection via the materialisation
step, so isolation is preserved.

:class:`AsyncEngine.iter_run` has a single-fragment fast path that
recognises the trivial merge shape that distributive federation emits
for one-backend plans (column rename + ORDER + LIMIT + identity
SUM-over-single-row groups + AVG decomposition) and executes the
merge in Python — skipping DuckDB's CREATE TABLE + INSERT roundtrip
plus the full second pass over the materialised rows.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Literal

import duckdb
import sqlglot
import sqlglot.errors
from semql.compile import ColumnMeta
from semql.federate import FederatedPlan
from semql.model import Backend
from sqlglot import expressions as exp

from semql_engine.adapter import Adapter, AdapterResult, AsyncAdapter


class EngineError(RuntimeError):
    """Raised by the engine when a plan can't be executed.

    Distinct from ``FederationError`` (compile-time refusals): this
    surfaces runtime issues such as a missing adapter for a backend the
    plan references, or an adapter returning rows whose columns don't
    match the fragment's declared output."""


@dataclass
class ExecutionResult:
    """Final result of running a :class:`FederatedPlan`.

    ``columns`` and ``column_meta`` are pass-throughs from the plan so a
    consumer that wants formatted output (units, percent, etc.) has
    everything it needs without re-resolving against the catalogue.
    """

    columns: list[str]
    column_meta: list[ColumnMeta]
    rows: list[tuple[Any, ...]]


class Engine:
    """Runs federated plans by materialising fragments into DuckDB.

    Register one adapter per backend you intend to query against, then
    call :meth:`run`. The engine isn't tied to a specific catalog;
    register adapters once and execute many plans.
    """

    def __init__(self, duckdb_connection: Any | None = None) -> None:  # noqa: ANN401
        self._con: Any = duckdb_connection or duckdb.connect(":memory:")
        self._adapters: dict[Backend, Adapter] = {}

    def register(self, backend: Backend, adapter: Adapter) -> None:
        """Bind an adapter to a backend. Replacing an existing
        registration is allowed (so callers can swap adapters mid-flight
        in tests)."""
        self._adapters[backend] = adapter

    def run(self, plan: FederatedPlan) -> ExecutionResult:
        """Execute a :class:`FederatedPlan` end-to-end.

        For each fragment, runs the SQL via the matching adapter and
        materialises the rows into a DuckDB temp table. Then runs the
        plan's merge SQL and returns the final rows + metadata.

        Raises :class:`EngineError` for missing adapters or column
        mismatches between adapter output and the fragment's declared
        columns.
        """
        self._reset_frag_tables(len(plan.fragments))
        for i, fragment in enumerate(plan.fragments):
            adapter = self._adapters.get(fragment.backend)
            if adapter is None:
                raise EngineError(
                    f"No adapter registered for backend "
                    f"{fragment.backend.value!r}. Call Engine.register("
                    f"Backend.{fragment.backend.name}, your_adapter) "
                    f"before running this plan."
                )
            result = adapter.execute(fragment.sql, fragment.params)
            if set(result.columns) != set(fragment.columns):
                raise EngineError(
                    f"Fragment {i} (backend {fragment.backend.value!r}) "
                    f"adapter returned columns {result.columns!r} but the "
                    f"fragment declares {fragment.columns!r}. Adapter "
                    f"must preserve the SELECT-list aliases."
                )
            materialised: list[tuple[Any, ...]] = [tuple(r) for r in result.rows]
            self._load_fragment(i, result.columns, materialised)

        merge_cursor = self._con.execute(plan.merge.sql, dict(plan.merge.params))
        rows = merge_cursor.fetchall()
        return ExecutionResult(
            columns=plan.columns,
            column_meta=plan.column_meta,
            rows=rows,
        )

    def iter_rows(self, plan: FederatedPlan) -> Iterator[dict[str, Any]]:
        """Convenience: run the plan and yield each row as a
        ``{column: value}`` dict. Useful for callers wiring the result
        into a templating layer / JSON envelope."""
        result = self.run(plan)
        for row in result.rows:
            yield dict(zip(result.columns, row, strict=True))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _reset_frag_tables(self, n: int) -> None:
        """Drop any frag_* tables left over from a previous run so we
        don't accidentally join against stale data. n is conservatively
        larger than needed in case a previous plan had more fragments."""
        # We drop generously to also clean up old runs with more frags.
        # A failed query won't recurse into Python-level state.
        for i in range(max(n, 32)):
            self._con.execute(f"DROP TABLE IF EXISTS frag_{i}")

    def _load_fragment(
        self,
        index: int,
        columns: list[str],
        rows: list[tuple[Any, ...]],
    ) -> None:
        """Materialise a fragment's rows into ``frag_<index>``.

        Strategy: infer a DuckDB type per column from the first non-NULL
        value in each column, CREATE TABLE with those types, then
        ``executemany`` the rows. Adapters that return empty result
        sets get a VARCHAR-typed table (we have no per-column type
        info in the adapter contract) — that's fine for merge joins
        that produce an empty result themselves."""
        col_idents = ", ".join(_quote(c) for c in columns)
        types = _infer_column_types(columns, rows)
        type_decls = ", ".join(f"{_quote(c)} {t}" for c, t in zip(columns, types, strict=True))
        self._con.execute(f"CREATE TABLE frag_{index} ({type_decls})")
        if not rows:
            return
        placeholders = ", ".join("?" for _ in columns)
        self._con.executemany(
            f"INSERT INTO frag_{index} ({col_idents}) VALUES ({placeholders})",
            rows,
        )


def _infer_column_types(columns: list[str], rows: list[tuple[Any, ...]]) -> list[str]:
    """Pick a DuckDB type per column from the first non-NULL value.

    Falls back to ``VARCHAR`` for fully-NULL columns and unknown types
    — DuckDB will widen on insert if the data is heterogeneous, and
    callers wanting strict types should cast on the source side."""
    types: list[str] = []
    for col_idx in range(len(columns)):
        chosen = "VARCHAR"
        for row in rows:
            v = row[col_idx]
            if v is None:
                continue
            chosen = _duckdb_type_for(v)
            break
        types.append(chosen)
    return types


def _duckdb_type_for(value: Any) -> str:  # noqa: ANN401 — any row value
    """Map a Python value to a DuckDB type literal.

    Order matters: ``bool`` is a subclass of ``int`` in Python, check
    it first."""
    import datetime as _dt

    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE"
    if isinstance(value, str):
        return "VARCHAR"
    if isinstance(value, _dt.datetime):
        return "TIMESTAMP"
    if isinstance(value, _dt.date):
        return "DATE"
    if isinstance(value, _dt.time):
        return "TIME"
    if isinstance(value, bytes):
        return "BLOB"
    return "VARCHAR"


def _quote(name: str) -> str:
    """DuckDB identifier quoting; matches semql.federate."""
    return f'"{name}"'


# ---------------------------------------------------------------------------
# Single-fragment fast path
# ---------------------------------------------------------------------------
#
# For a 1-fragment plan, the merge SQL is structurally trivial: it
# selects from ``frag_0`` with column renames, an identity SUM (or
# NULLIF(SUM/SUM) for AVG) since each group has exactly one row in the
# fragment, plus optional ORDER BY / LIMIT / OFFSET. There is no
# cross-fragment join to do.
#
# Going through DuckDB still works but pays for: a CREATE TABLE +
# INSERT roundtrip that copies every row, plus a second full pass when
# the merge SELECT scans frag_0. For large raw-row results this
# overhead is real. The fast path parses the merge SQL once, builds a
# tiny *MergeProgram* (per-output-column transform + sort + slice),
# and applies it to the adapter rows directly — no DuckDB touched.
#
# Detection is conservative: any merge shape we don't immediately
# recognise (HAVING, complex expressions, cross-fragment JOIN, etc.)
# returns ``None`` from ``_try_build_program`` and we fall through to
# the DuckDB path. Correctness > coverage.


@dataclass(frozen=True)
class _ColTransform:
    """How to compute one output column from a fragment row.

    ``kind`` is one of:
    - ``"identity"`` — copy ``f0.<src>`` to the output. Covers both
      bare dimension references and ``SUM(col)`` over single-row groups
      (SUM of one element is identity, modulo NULL handling: SUM
      ignores NULLs whereas the identity path passes them through;
      single-fragment plans don't produce per-group NULL aggregates
      so this is safe).
    - ``"avg_div"`` — emit ``sum_col / count_col`` when ``count_col``
      is non-zero / non-null; else NULL. Mirrors the
      ``NULLIF(SUM(sum_col), 0)`` shape the merge SQL uses for AVG
      decomposition.
    """

    kind: Literal["identity", "avg_div"]
    src_col: str = ""  # for identity
    sum_col: str = ""  # for avg_div
    count_col: str = ""  # for avg_div


@dataclass(frozen=True)
class _MergeProgram:
    """Recipe for executing a single-fragment merge in Python.

    ``transforms`` is parallel to the plan's output columns. ``order``
    is a list of ``(output_col_index, descending)`` pairs applied as a
    stable Python sort. ``limit`` and ``offset`` apply after sort.
    """

    transforms: list[_ColTransform]
    order: list[tuple[int, bool]] = dc_field(default_factory=lambda: [])
    limit: int | None = None
    offset: int | None = None


def _try_build_program(
    merge_sql: str,
    fragment_columns: list[str],
    output_columns: list[str],
) -> _MergeProgram | None:
    """Parse a single-fragment merge SQL into an executable program.

    Returns ``None`` if the SQL doesn't match the trivial single-fragment
    shape the federation layer emits — caller falls back to the DuckDB
    materialisation path."""
    try:
        tree = sqlglot.parse_one(merge_sql, dialect="duckdb")
    except sqlglot.errors.ParseError:
        return None
    if not isinstance(tree, exp.Select):
        return None

    # HAVING / DISTINCT / WINDOW / CTEs aren't part of the trivial shape.
    if tree.args.get("having") is not None:
        return None
    if tree.args.get("distinct") is not None:
        return None
    if tree.args.get("with") is not None:
        return None

    select_exprs = tree.expressions

    # Star: ``SELECT * FROM frag_0`` — identity over every fragment
    # column in fragment order. The federation layer emits this for
    # the trivial single-backend case (no measure renames needed).
    transforms: list[_ColTransform] = []
    fragment_set = set(fragment_columns)
    if len(select_exprs) == 1 and isinstance(select_exprs[0], exp.Star):
        if fragment_columns != output_columns:
            return None
        transforms = [_ColTransform(kind="identity", src_col=c) for c in fragment_columns]
    else:
        if len(select_exprs) != len(output_columns):
            return None
        for expression in select_exprs:
            t = _try_parse_select_item(expression, fragment_set)
            if t is None:
                return None
            transforms.append(t)

    # ORDER BY — output-column references only.
    order_pairs: list[tuple[int, bool]] = []
    order_node = tree.args.get("order")
    if order_node is not None:
        for ordered in order_node.expressions:
            if not isinstance(ordered, exp.Ordered):
                return None
            col = ordered.this
            if not isinstance(col, exp.Column) or col.table:
                return None
            name = col.name
            if name not in output_columns:
                return None
            order_pairs.append((output_columns.index(name), bool(ordered.args.get("desc"))))

    limit_val: int | None = None
    limit_node = tree.args.get("limit")
    if limit_node is not None:
        n = limit_node.expression if hasattr(limit_node, "expression") else None
        if isinstance(n, exp.Literal) and n.is_int:
            limit_val = int(n.this)
        else:
            return None

    offset_val: int | None = None
    offset_node = tree.args.get("offset")
    if offset_node is not None:
        n = offset_node.expression if hasattr(offset_node, "expression") else None
        if isinstance(n, exp.Literal) and n.is_int:
            offset_val = int(n.this)
        else:
            return None

    return _MergeProgram(
        transforms=transforms,
        order=order_pairs,
        limit=limit_val,
        offset=offset_val,
    )


def _try_parse_select_item(
    expression: Any,  # noqa: ANN401 — sqlglot Expression isn't in the public `expressions` __all__
    fragment_cols: set[str],
) -> _ColTransform | None:
    """Match the merge SQL's SELECT items: ``f0.col AS out``,
    ``SUM(f0.col) AS out``, or ``SUM(f0.sum_col) / NULLIF(SUM(f0.count_col), 0) AS out``."""
    inner = expression.unalias()

    # Bare column reference: ``f0.col`` (or just ``col``).
    if isinstance(inner, exp.Column):
        col = inner.name
        if col in fragment_cols:
            return _ColTransform(kind="identity", src_col=col)
        return None

    # ``SUM(f0.col)`` — identity over single-row groups.
    if isinstance(inner, exp.Sum):
        arg = inner.this
        if isinstance(arg, exp.Column) and arg.name in fragment_cols:
            return _ColTransform(kind="identity", src_col=arg.name)
        return None

    # AVG decomposition: ``SUM(sum_col) / NULLIF(SUM(count_col), 0)``.
    if isinstance(inner, exp.Div):
        num = inner.this
        den = inner.expression
        if not (isinstance(num, exp.Sum) and isinstance(num.this, exp.Column)):
            return None
        if not isinstance(den, exp.Nullif):
            return None
        den_sum = den.this
        zero = den.expression
        if not (isinstance(den_sum, exp.Sum) and isinstance(den_sum.this, exp.Column)):
            return None
        if not (isinstance(zero, exp.Literal) and zero.this == "0"):
            return None
        sum_col = num.this.name
        count_col = den_sum.this.name
        if sum_col in fragment_cols and count_col in fragment_cols:
            return _ColTransform(
                kind="avg_div",
                sum_col=sum_col,
                count_col=count_col,
            )
    return None


def _project_row(
    row: Sequence[Any],
    col_index: dict[str, int],
    transforms: list[_ColTransform],
) -> tuple[Any, ...]:
    """Apply a MergeProgram's column transforms to one fragment row."""
    out: list[Any] = []
    for t in transforms:
        if t.kind == "identity":
            out.append(row[col_index[t.src_col]])
        else:
            sum_val = row[col_index[t.sum_col]]
            count_val = row[col_index[t.count_col]]
            if sum_val is None or count_val in (None, 0):
                out.append(None)
            else:
                out.append(sum_val / count_val)
    return tuple(out)


def _apply_program(
    program: _MergeProgram,
    fragment_columns: list[str],
    rows: Iterable[Sequence[Any]],
) -> list[tuple[Any, ...]]:
    """Execute the MergeProgram against the fragment rowset.

    Returns the projected + sorted + sliced result list. Sorting +
    slicing forces materialisation; chunk emission still lets callers
    stream the OUTPUT side."""
    col_index = {c: i for i, c in enumerate(fragment_columns)}
    projected = [_project_row(r, col_index, program.transforms) for r in rows]
    if program.order:
        for col_idx, descending in reversed(program.order):

            def _key(row: tuple[Any, ...], _i: int = col_idx) -> _OrderKey:
                return _OrderKey(row[_i])

            projected.sort(key=_key, reverse=descending)
    if program.offset:
        projected = projected[program.offset :]
    if program.limit is not None:
        projected = projected[: program.limit]
    return projected


class _OrderKey:
    """Wrapper so Python sort handles NULLs without raising.

    Python ``sorted`` over a list containing ``None`` and numbers
    raises ``TypeError``. SQL ORDER BY treats NULL as low (ASC) by
    default. We mirror that — NULLs sort first; non-NULLs follow
    in natural Python order."""

    __slots__ = ("v",)

    def __init__(self, v: Any) -> None:  # noqa: ANN401
        self.v = v

    def __lt__(self, other: _OrderKey) -> bool:
        a, b = self.v, other.v
        if a is None and b is None:
            return False
        if a is None:
            return True
        if b is None:
            return False
        return bool(a < b)


_FRAG_TABLE_RE = re.compile(r"\bfrag_(\d+)\b")


class AsyncEngine:
    """Async counterpart to :class:`Engine`.

    Runs federated plans by awaiting per-fragment adapters in parallel
    via :func:`asyncio.gather`, then merging the results in DuckDB.
    Fragments of a single ``FederatedPlan`` are always independent
    (they're per-backend sub-queries; the join lives in the merge SQL),
    so the parallelism is safe for any plan the federation layer
    produces.

    :meth:`iter_run` adds chunked streaming: the merge cursor's rows
    are fetched in batches of ``chunk_rows`` so a result set with
    millions of rows doesn't have to land in memory all at once. For
    single-fragment plans, the merge runs in Python (DuckDB skipped
    entirely) — see ``_MergeProgram`` and ``_try_build_program`` for
    the recognised shapes. Multi-fragment plans continue to merge in
    DuckDB because that's where the join belongs.

    ``last_iter_run_used_fast_path`` records which path the most-recent
    ``iter_run`` call took. Useful for tests + observability; not part
    of the wire protocol.
    """

    def __init__(self, duckdb_connection: Any | None = None) -> None:  # noqa: ANN401
        self._con: Any = duckdb_connection or duckdb.connect(":memory:")
        self._adapters: dict[Backend, AsyncAdapter] = {}
        self.last_iter_run_used_fast_path: bool = False

    def register(self, backend: Backend, adapter: AsyncAdapter) -> None:
        """Bind an async adapter to a backend. Replacing an existing
        registration is allowed."""
        self._adapters[backend] = adapter

    async def run(self, plan: FederatedPlan) -> ExecutionResult:
        """Execute a :class:`FederatedPlan` end-to-end on an event loop.

        Fragments are launched concurrently via :func:`asyncio.gather`;
        a single slow adapter doesn't block the others. Once every
        fragment has returned, results are materialised into DuckDB and
        the merge SQL runs to produce the final shape.

        Raises :class:`EngineError` for missing adapters or column
        mismatches.
        """
        self._adapters_present(plan)
        self._reset_frag_tables(len(plan.fragments))

        results = await asyncio.gather(
            *(
                self._adapters[frag.backend].execute(frag.sql, frag.params)
                for frag in plan.fragments
            )
        )

        for i, (fragment, result) in enumerate(zip(plan.fragments, results, strict=True)):
            self._load_result(i, fragment, result)

        merge_cursor = self._con.execute(plan.merge.sql, dict(plan.merge.params))
        rows = merge_cursor.fetchall()
        return ExecutionResult(
            columns=plan.columns,
            column_meta=plan.column_meta,
            rows=rows,
        )

    async def iter_run(
        self,
        plan: FederatedPlan,
        *,
        chunk_rows: int = 10_000,
    ) -> AsyncIterator[list[tuple[Any, ...]]]:
        """Run ``plan`` and yield merge result rows in chunks.

        Two paths:

        - **Single-fragment fast path** — when ``plan.fragments`` has
          one entry and ``_try_build_program`` recognises the merge
          shape, the merge runs in Python without DuckDB.
          ``last_iter_run_used_fast_path`` is set to ``True``.
        - **DuckDB merge** — multi-fragment plans, or shapes the fast
          path doesn't recognise (HAVING etc.). Fragments materialise
          into DuckDB temp tables and the merge cursor is fetched via
          ``fetchmany`` for memory-bounded streaming.

        Yields a list of row tuples per iteration; an empty list is
        never emitted — the iterator terminates instead.
        """
        if chunk_rows <= 0:
            raise EngineError(f"iter_run: chunk_rows must be positive, got {chunk_rows!r}.")
        self._adapters_present(plan)
        self.last_iter_run_used_fast_path = False

        if len(plan.fragments) == 1:
            fragment = plan.fragments[0]
            program = _try_build_program(
                plan.merge.sql,
                fragment.columns,
                plan.columns,
            )
            if program is not None:
                self.last_iter_run_used_fast_path = True
                adapter = self._adapters[fragment.backend]
                result = await adapter.execute(fragment.sql, fragment.params)
                if set(result.columns) != set(fragment.columns):
                    raise EngineError(
                        f"Fragment 0 (backend {fragment.backend.value!r}) "
                        f"adapter returned columns {result.columns!r} but "
                        f"the fragment declares {fragment.columns!r}. "
                        "Adapter must preserve the SELECT-list aliases."
                    )
                rows = _apply_program(program, result.columns, result.rows)
                for start in range(0, len(rows), chunk_rows):
                    yield rows[start : start + chunk_rows]
                return

        self._reset_frag_tables(len(plan.fragments))

        results = await asyncio.gather(
            *(
                self._adapters[frag.backend].execute(frag.sql, frag.params)
                for frag in plan.fragments
            )
        )
        for i, (fragment, result) in enumerate(zip(plan.fragments, results, strict=True)):
            self._load_result(i, fragment, result)

        cursor = self._con.execute(plan.merge.sql, dict(plan.merge.params))
        while True:
            chunk = await asyncio.to_thread(cursor.fetchmany, chunk_rows)
            if not chunk:
                return
            yield [tuple(row) for row in chunk]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _adapters_present(self, plan: FederatedPlan) -> None:
        for frag in plan.fragments:
            if frag.backend not in self._adapters:
                raise EngineError(
                    f"No adapter registered for backend "
                    f"{frag.backend.value!r}. Call AsyncEngine.register("
                    f"Backend.{frag.backend.name}, your_adapter) before "
                    f"running this plan."
                )

    def _load_result(self, index: int, fragment: Any, result: AdapterResult) -> None:  # noqa: ANN401
        if set(result.columns) != set(fragment.columns):
            raise EngineError(
                f"Fragment {index} (backend {fragment.backend.value!r}) "
                f"adapter returned columns {result.columns!r} but the "
                f"fragment declares {fragment.columns!r}. Adapter "
                f"must preserve the SELECT-list aliases."
            )
        materialised: list[tuple[Any, ...]] = [tuple(r) for r in result.rows]
        # Reuse Engine's loader; signature matches.
        Engine._load_fragment(self, index, result.columns, materialised)  # type: ignore[arg-type]

    def _reset_frag_tables(self, n: int) -> None:
        Engine._reset_frag_tables(self, n)  # type: ignore[arg-type]


__all__ = ["AsyncEngine", "Engine", "EngineError", "ExecutionResult"]
