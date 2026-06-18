"""Row-mode reads: ``fetch`` (one row by key) and ``list`` (allowlisted
short lists) over entities.

This is the read escape hatch from the analytic layer (see
``docs/specs/entities.md`` M2). An :class:`~semql.model.Entity` declares
the business object; ``compile_fetch`` / ``compile_list`` turn an
``EntityFetch`` / ``EntityList`` request into a :class:`CompiledEntityQuery`.

The design is **lower-then-derive** (decision D8): every read lowers to a
``SemanticQuery(ungrouped=True, ...)`` and reuses ``compile_query`` for the
SQL path — inheriting scope injection, the bind-as-parameter discipline,
join flattening and the existing refusals verbatim. In parallel a
restricted, serialisable :class:`RowPlan` is derived from the same request
so a non-SQL (row-capable) adapter can interpret it without seeing SQL.

Pagination (D9) is keyset, executor-owned and opaque to clients. For SQL
backends the cursor is decoded into a keyset ``WHERE`` at compile time
(parsing, not I/O); for custom backends the token rides ``RowPlan.cursor``
verbatim and the adapter owns it.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from semql.errors import CompileError
from semql.model import AuthContext, Cube, Entity
from semql.refs import field_of
from semql.spec import BoolExpr, Filter, SemanticQuery, TimeWindow

if TYPE_CHECKING:
    from semql.catalog import Catalog

Value = str | int | float | bool

__all__ = [
    "CompiledEntityQuery",
    "EntityFetch",
    "EntityList",
    "RowPlan",
    "RowPred",
    "SourceRef",
    "compile_fetch",
    "compile_list",
    "decode_cursor",
    "encode_cursor",
    "non_portable_entities",
]


# ---------------------------------------------------------------------------
# Request specs
# ---------------------------------------------------------------------------


class EntityFetch(BaseModel):
    """Fetch exactly one row of an entity by its key (D5)."""

    model_config = ConfigDict(frozen=True)
    entity: str
    key: str | int
    fields: list[str] | None = Field(
        default=None,
        description="Subset of the entity's output fields; None returns all.",
    )


class EntityList(BaseModel):
    """List rows of an entity through allowlisted filters (D5/D10).

    ``where`` keys and the ``time_range`` dimension must be in the
    entity's ``list_filters`` allowlist, or compilation refuses."""

    model_config = ConfigDict(frozen=True)
    entity: str
    where: dict[str, Value | list[Value]] = Field(default_factory=dict)
    # (time_dim, start, end) — half-open [start, end); the dim must be
    # allowlisted in ``list_filters``.
    time_range: tuple[str, str, str] | None = None
    order: str | None = None
    limit: int = 50
    cursor: str | None = None


# ---------------------------------------------------------------------------
# RowPlan — the restricted, serialisable plan adapters interpret
# ---------------------------------------------------------------------------


class SourceRef(BaseModel):
    """Where an entity's rows physically live."""

    model_config = ConfigDict(frozen=True)
    cube: str
    table: str
    backend: str


class RowPred(BaseModel):
    """A single restricted predicate the row-mode plan carries.

    ``params`` names the bind-parameter(s) whose values live in
    ``RowPlan.params`` — ``eq``/``in`` carry one, ``time_range`` carries
    two (start, end). Values are never inlined (the bind-never-inline
    invariant, A.4.2)."""

    model_config = ConfigDict(frozen=True)
    column: str
    op: Literal["eq", "in", "time_range"]
    params: list[str]


class RowPlan(BaseModel):
    """A serialisable, restricted plan for one row-mode read.

    The *type* enforces the restricted vocabulary (eq / in / time_range);
    a row-capable adapter interprets it without ever touching SQL.
    ``scope_predicates`` carries only *structured* scope (e.g. discriminator
    tenancy) — raw-SQL scope is non-portable and is refused for
    custom-backend entities before a plan is built."""

    model_config = ConfigDict(frozen=True)
    version: int = 1
    source: SourceRef
    columns: list[str]
    predicates: list[RowPred] = Field(default_factory=lambda: list[RowPred]())
    scope_predicates: list[RowPred] = Field(default_factory=lambda: list[RowPred]())
    order: list[tuple[str, Literal["asc", "desc"]]] = Field(
        default_factory=lambda: list[tuple[str, Literal["asc", "desc"]]]()
    )
    limit: int = 50
    cursor: str | None = None
    # Bind values for every param named in predicates/scope_predicates.
    # Self-contained so an adapter needs nothing but this plan.
    params: dict[str, Any] = Field(default_factory=lambda: dict[str, Any]())

    @model_validator(mode="after")
    def _check_params_resolve(self) -> RowPlan:
        named = {p for pred in (*self.predicates, *self.scope_predicates) for p in pred.params}
        missing = named - set(self.params)
        if missing:
            raise ValueError(f"RowPlan references unbound params: {sorted(missing)}.")
        return self


class CompiledEntityQuery(BaseModel):
    """The output of ``compile_fetch`` / ``compile_list``.

    ``sql`` is populated for SQL backends (``params`` are then the SQL's
    bound params); for a custom-backend entity ``sql`` is ``None`` and the
    adapter executes ``plan``. ``plan`` is always present so the SQL and
    plan paths can be golden-tested against each other (M3)."""

    model_config = ConfigDict(frozen=True)
    plan: RowPlan
    sql: str | None
    params: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Cursor codec (opaque keyset token)
# ---------------------------------------------------------------------------


def encode_cursor(values: list[Any]) -> str:
    """Encode keyset anchor values into an opaque pagination token.

    The executor (semql-engine) calls this with the last row's ordered
    values + key to mint the next page's cursor."""
    raw = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(token: str) -> list[Any]:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        decoded = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise CompileError(f"Malformed pagination cursor: {token!r}.") from exc
    if not isinstance(decoded, list):
        raise CompileError(f"Malformed pagination cursor (expected a list): {token!r}.")
    # pyright narrows json.loads' result to list[Unknown]; the cast pins it
    # to list[Any] (mypy already sees list[Any], hence the redundant-cast).
    return cast("list[Any]", decoded)  # type: ignore[redundant-cast]


# ---------------------------------------------------------------------------
# Portability lint (§4 auth-portability rule)
# ---------------------------------------------------------------------------


def _cube_has_raw_scope(cube: Cube) -> bool:
    """A cube's scope is non-portable when it relies on raw SQL — either a
    ``security_sql`` fragment or a named ``scope`` ScopeFn (whose output is
    a raw ``ScopePredicate.sql``). Structured discriminator/schema tenancy
    is portable and does not count."""
    return bool(cube.security_sql) or cube.scope is not None


def non_portable_entities(catalog: Catalog) -> list[str]:
    """Lint: names of custom-backend entities whose cube uses raw-SQL scope.

    These would be refused by ``compile_fetch``/``compile_list``; the lint
    surfaces them up front (e.g. for a catalog health check)."""
    by_name = catalog.as_dict()
    out: list[str] = []
    for e in catalog.entities.values():
        if not e.custom_backend:
            continue
        if any(_cube_has_raw_scope(by_name[c]) for c in e.cubes if c in by_name):
            out.append(e.name)
    return out


# ---------------------------------------------------------------------------
# Shared lowering helpers
# ---------------------------------------------------------------------------


def _resolve_entity(name: str, catalog: Catalog) -> Entity:
    entity = catalog.entities.get(name)
    if entity is None:
        raise CompileError(f"Unknown entity {name!r}. Known entities: {sorted(catalog.entities)}.")
    if entity.key is None:
        raise CompileError(f"Entity {name!r} is vocabulary-only (no key) — fetch/list need a key.")
    return entity


def _output_fields(entity: Entity, catalog: Catalog) -> dict[str, str]:
    """Return ``{output_name: qualified_ref}`` for the entity's columns.

    Uses the explicit ``fields`` rename map when present; otherwise every
    plain dimension on the primary cube, named by field name. Time
    dimensions are deliberately excluded from the default projection — the
    analytic compiler treats them as bucket sources, not selectable plain
    columns — but they remain usable as ``list_filters`` (time_range) and
    ``default_order`` anchors. Project a time column explicitly via the
    entity's ``fields`` map only if the cube also exposes it as a plain
    dimension."""
    if entity.fields:
        return dict(entity.fields)
    primary = catalog.as_dict()[entity.cubes[0]]
    return {d.name: f"{primary.name}.{d.name}" for d in primary.dimensions}


def _refuse_raw_scope_if_custom(entity: Entity, catalog: Catalog) -> None:
    if not entity.custom_backend:
        return
    by_name = catalog.as_dict()
    offenders = [c for c in entity.cubes if c in by_name and _cube_has_raw_scope(by_name[c])]
    if offenders:
        raise CompileError(
            f"Entity {entity.name!r} targets a custom (non-SQL) backend but its "
            f"cube(s) {offenders} carry raw-SQL scope (security_sql / scope). "
            "Raw SQL cannot be ported to a non-SQL adapter; use structured "
            "(discriminator) tenancy, or drop custom_backend."
        )


def _source_ref(entity: Entity, catalog: Catalog) -> SourceRef:
    primary = catalog.as_dict()[entity.cubes[0]]
    return SourceRef(cube=primary.name, table=primary.table, backend=primary.dialect.value)


def _scope_predicates(
    entity: Entity, catalog: Catalog, viewer: AuthContext | None
) -> tuple[list[RowPred], dict[str, Any]]:
    """Structured scope for the plan path: discriminator tenancy only.

    Raw-SQL scope never reaches here (custom-backend entities are refused
    upstream; SQL-backend entities carry it in the rendered SQL instead)."""
    preds: list[RowPred] = []
    params: dict[str, Any] = {}
    primary = catalog.as_dict()[entity.cubes[0]]
    if primary.tenancy == "discriminator" and viewer is not None and viewer.tenant is not None:
        for i, col in enumerate(primary.tenancy_columns):
            pname = f"scope_{i}"
            preds.append(RowPred(column=col, op="eq", params=[pname]))
            params[pname] = viewer.tenant
    return preds, params


# ---------------------------------------------------------------------------
# compile_fetch
# ---------------------------------------------------------------------------


def compile_fetch(
    spec: EntityFetch,
    catalog: Catalog,
    *,
    viewer: AuthContext | None = None,
    context: dict[str, str] | None = None,
) -> CompiledEntityQuery:
    """Compile a point lookup of one entity row by key."""
    entity = _resolve_entity(spec.entity, catalog)
    _refuse_raw_scope_if_custom(entity, catalog)

    outputs = _output_fields(entity, catalog)
    selected = list(outputs) if spec.fields is None else spec.fields
    unknown = [f for f in selected if f not in outputs]
    if unknown:
        raise CompileError(
            f"EntityFetch on {entity.name!r}: unknown field(s) {unknown}. Known: {sorted(outputs)}."
        )

    assert entity.key is not None  # _resolve_entity guarantees it
    key_pred = RowPred(column=field_of(entity.key), op="eq", params=["key"])
    scope_preds, scope_params = _scope_predicates(entity, catalog, viewer)
    plan = RowPlan(
        source=_source_ref(entity, catalog),
        columns=selected,
        predicates=[key_pred],
        scope_predicates=scope_preds,
        order=[],
        limit=1,
        params={"key": spec.key, **scope_params},
    )

    if entity.custom_backend:
        return CompiledEntityQuery(plan=plan, sql=None, params=plan.params, columns=selected)

    query = SemanticQuery(
        ungrouped=True,
        dimensions=[outputs[f] for f in selected],
        filters=[Filter(dimension=entity.key, op="eq", values=[spec.key])],
        aliases=_alias_map(outputs, selected),
        limit=1,
    )
    compiled = catalog.compile(query, context=context, viewer=viewer)
    return CompiledEntityQuery(
        plan=plan, sql=compiled.sql, params=compiled.params, columns=list(compiled.columns)
    )


# ---------------------------------------------------------------------------
# compile_list
# ---------------------------------------------------------------------------


def compile_list(
    spec: EntityList,
    catalog: Catalog,
    *,
    viewer: AuthContext | None = None,
    context: dict[str, str] | None = None,
) -> CompiledEntityQuery:
    """Compile an allowlisted short list of an entity's rows."""
    entity = _resolve_entity(spec.entity, catalog)
    _refuse_raw_scope_if_custom(entity, catalog)

    outputs = _output_fields(entity, catalog)
    allowed = set(entity.list_filters)

    # Allowlist enforcement (D5/D10): every where key + the time_range dim
    # must be explicitly allowlisted.
    where_filters: list[Filter] = []
    row_preds: list[RowPred] = []
    plan_params: dict[str, Any] = {}
    for i, (ref, value) in enumerate(spec.where.items()):
        if ref not in allowed:
            raise CompileError(
                f"EntityList on {entity.name!r}: filter {ref!r} is not in the "
                f"entity's list_filters allowlist {sorted(allowed)}. Richer "
                "filtering routes to the analytic layer."
            )
        pname = f"w{i}"
        if isinstance(value, list):
            where_filters.append(Filter(dimension=ref, op="in", values=value))
            row_preds.append(RowPred(column=field_of(ref), op="in", params=[pname]))
            plan_params[pname] = value
        else:
            where_filters.append(Filter(dimension=ref, op="eq", values=[value]))
            row_preds.append(RowPred(column=field_of(ref), op="eq", params=[pname]))
            plan_params[pname] = value

    time_window: TimeWindow | None = None
    if spec.time_range is not None:
        tdim, start, end = spec.time_range
        if tdim not in allowed:
            raise CompileError(
                f"EntityList on {entity.name!r}: time_range dimension {tdim!r} is "
                f"not in list_filters {sorted(allowed)}."
            )
        time_window = TimeWindow(dimension=tdim, range=(start, end))
        row_preds.append(
            RowPred(column=field_of(tdim), op="time_range", params=["t_start", "t_end"])
        )
        plan_params["t_start"] = start
        plan_params["t_end"] = end

    # Order: explicit override else the entity default; append the key as a
    # tiebreaker for a total order (keyset requirement, D9).
    order_spec = spec.order or entity.default_order
    order_pairs = _parse_order(order_spec) if order_spec else []
    assert entity.key is not None
    key_pair: tuple[str, Literal["asc", "desc"]] = (entity.key, "asc")
    if not any(col == entity.key for col, _ in order_pairs):
        order_pairs = [*order_pairs, key_pair]

    limit = min(spec.limit, catalog.max_list_limit)

    scope_preds, scope_params = _scope_predicates(entity, catalog, viewer)
    plan = RowPlan(
        source=_source_ref(entity, catalog),
        columns=list(outputs),
        predicates=row_preds,
        scope_predicates=scope_preds,
        order=[(field_of(c), d) for c, d in order_pairs],
        limit=limit,
        cursor=spec.cursor if entity.custom_backend else None,
        params={**plan_params, **scope_params},
    )

    if entity.custom_backend:
        return CompiledEntityQuery(plan=plan, sql=None, params=plan.params, columns=list(outputs))

    # SQL path: decode the cursor into keyset predicates (parsing, not I/O).
    keyset_filters, keyset_tree = (
        _keyset_where(spec.cursor, order_pairs) if spec.cursor else ([], None)
    )
    query = SemanticQuery(
        ungrouped=True,
        dimensions=[outputs[f] for f in outputs],
        filters=[*where_filters, *keyset_filters],
        where=keyset_tree,
        time_dimension=time_window,
        aliases=_alias_map(outputs, list(outputs)),
        order=order_pairs,
        limit=limit,
    )
    compiled = catalog.compile(query, context=context, viewer=viewer)
    return CompiledEntityQuery(
        plan=plan, sql=compiled.sql, params=compiled.params, columns=list(compiled.columns)
    )


# ---------------------------------------------------------------------------
# small parsers
# ---------------------------------------------------------------------------


def _parse_order(order: str) -> list[tuple[str, Literal["asc", "desc"]]]:
    parts = order.split()
    col = parts[0]
    is_desc = len(parts) == 2 and parts[1].lower() == "desc"
    direction: Literal["asc", "desc"] = "desc" if is_desc else "asc"
    return [(col, direction)]


def _alias_map(outputs: dict[str, str], selected: list[str]) -> dict[str, str]:
    """Alias each qualified ref back to its local output name, but only when
    the local name differs from the field's own name (else the compiler's
    default column name already matches)."""
    aliases: dict[str, str] = {}
    for local in selected:
        qualified = outputs[local]
        if field_of(qualified) != local:
            aliases[local] = qualified
    return aliases


def _keyset_where(
    cursor: str, order_pairs: list[tuple[str, Literal["asc", "desc"]]]
) -> tuple[list[Filter], BoolExpr | None]:
    """Decode a keyset token into a "row strictly after the cursor" predicate.

    Returns ``(filters, tree)``: a single-column keyset is one ``Filter``
    (added to the flat filters list); a multi-column keyset is the
    lexicographic ``(c0 cmp v0) OR (c0 = v0 AND (c1 cmp v1) OR ...)`` tree.
    Values bind as parameters via the normal filter path (never inlined)."""
    values = decode_cursor(cursor)
    if len(values) != len(order_pairs):
        raise CompileError(
            f"Pagination cursor has {len(values)} value(s) but the order has "
            f"{len(order_pairs)} column(s)."
        )

    def _cmp(direction: str) -> Literal["gt", "lt"]:
        return "gt" if direction == "asc" else "lt"

    if len(order_pairs) == 1:
        col, direction = order_pairs[0]
        return [Filter(dimension=col, op=_cmp(direction), values=[values[0]])], None

    # Build right-to-left: tail predicate for the last column, then wrap.
    col, direction = order_pairs[-1]
    node: BoolExpr | Filter = Filter(dimension=col, op=_cmp(direction), values=[values[-1]])
    for (col, direction), val in zip(
        reversed(order_pairs[:-1]), reversed(values[:-1]), strict=True
    ):
        strict = Filter(dimension=col, op=_cmp(direction), values=[val])
        eq = Filter(dimension=col, op="eq", values=[val])
        node = BoolExpr(op="or", children=[strict, BoolExpr(op="and", children=[eq, node])])
    assert isinstance(node, BoolExpr)
    return [], node
