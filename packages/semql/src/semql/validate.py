"""Collect-all static validation of a SemanticQuery against a catalog.

``compile_query`` fails at the first problem with a CompileError;
``validate`` collects every problem it can find and returns them as a
list of ``ValidationError`` records. Two contracts (PHILOSOPHY.md):

- ``compile()`` is for the hot path — fail-fast, structured exception.
- ``validate()`` is for the planner-feedback path — surface everything
  the user / LLM needs to fix in one round-trip.

``validate`` never raises on input it would otherwise complain about.
It returns an empty list when the query is compile-ready.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as _dc_field
from typing import TYPE_CHECKING, Any

from semql._resolve import resolve_field
from semql.errors import UnknownIdentifierError, closest_match
from semql.model import Cube, Dimension, Measure, TimeDimension
from semql.spec import SemanticQuery

if TYPE_CHECKING:
    from semql.catalog import Catalog

MAX_UNGROUPED_ROWS = 1000


ErrorCode = str  # documented values listed in this module's docstring


@dataclass(frozen=True)
class ValidationError:
    """One problem with a query.

    ``code`` is a stable identifier callers can branch on (see this
    module's leading docstring for the catalog of codes). ``message``
    is a human-readable explanation. The remaining fields carry the
    structure the message refers to — populated when applicable.
    """

    code: ErrorCode
    message: str
    cube: str | None = None
    field: str | None = None
    op: str | None = None
    value: Any = None
    hint: str | None = None
    extra: dict[str, Any] = _dc_field(default_factory=dict[str, Any])


def _catalog_dict(catalog: Catalog | dict[str, Cube]) -> dict[str, Cube]:
    if isinstance(catalog, dict):
        return catalog
    return catalog.as_dict()


def _resolve_silent(
    qualified: str,
    catalog: dict[str, Cube],
) -> tuple[Cube, Measure | Dimension | TimeDimension] | ValidationError:
    """Like ``resolve_field`` but returns a ValidationError instead of
    raising. Other resolve failures (malformed reference) surface as a
    generic unknown_field error."""
    try:
        return resolve_field(qualified, catalog)
    except UnknownIdentifierError as exc:
        code = "unknown_cube" if exc.kind == "cube" else "unknown_field"
        return ValidationError(
            code=code,
            message=str(exc),
            cube=exc.cube,
            field=exc.name if exc.kind == "field" else None,
            hint=exc.hint,
        )
    except Exception as exc:
        return ValidationError(
            code="bad_reference",
            message=str(exc),
            field=qualified,
        )


def validate(
    query: SemanticQuery,
    catalog: Catalog | dict[str, Cube],
) -> list[ValidationError]:
    """Return every problem the static checker can find in ``query``.

    Returns ``[]`` for a query that ``compile_query`` would also accept.
    Never raises on input.
    """
    cat = _catalog_dict(catalog)
    errors: list[ValidationError] = []

    if not query.measures and not query.dimensions and query.time_dimension is None:
        errors.append(
            ValidationError(
                code="empty_query",
                message=(
                    "SemanticQuery is empty — at least one measure, "
                    "dimension, or time_dimension is required."
                ),
            )
        )

    touched: list[Cube] = []

    def _track(c: Cube) -> None:
        if c not in touched:
            touched.append(c)

    for ref in query.measures:
        result = _resolve_silent(ref, cat)
        if isinstance(result, ValidationError):
            errors.append(result)
            continue
        c, fld = result
        _track(c)
        if not isinstance(fld, Measure):
            errors.append(
                ValidationError(
                    code="wrong_field_kind",
                    message=f"{ref!r} is not a measure on cube {c.name!r}.",
                    cube=c.name,
                    field=fld.name,
                )
            )

    for ref in query.dimensions:
        result = _resolve_silent(ref, cat)
        if isinstance(result, ValidationError):
            errors.append(result)
            continue
        c, fld = result
        _track(c)
        if not isinstance(fld, Dimension):
            errors.append(
                ValidationError(
                    code="wrong_field_kind",
                    message=f"{ref!r} is not a dimension on cube {c.name!r}.",
                    cube=c.name,
                    field=fld.name,
                )
            )

    time_dim_field: TimeDimension | None = None
    if query.time_dimension is not None:
        tref = query.time_dimension.dimension
        result = _resolve_silent(tref, cat)
        if isinstance(result, ValidationError):
            errors.append(result)
        else:
            c, fld = result
            _track(c)
            if not isinstance(fld, TimeDimension):
                errors.append(
                    ValidationError(
                        code="wrong_field_kind",
                        message=f"{tref!r} is not a time dimension.",
                        cube=c.name,
                        field=fld.name,
                    )
                )
            else:
                time_dim_field = fld
                gran = query.time_dimension.granularity
                if gran is not None and gran not in fld.granularities:
                    errors.append(
                        ValidationError(
                            code="bad_granularity",
                            message=(
                                f"Granularity {gran!r} not supported on {tref!r}. "
                                f"Allowed: {fld.granularities}."
                            ),
                            field=tref,
                            value=gran,
                            extra={"allowed": list(fld.granularities)},
                        )
                    )

    for f in query.filters:
        result = _resolve_silent(f.dimension, cat)
        if isinstance(result, ValidationError):
            errors.append(result)
            continue
        c, fld = result
        _track(c)
        fld_type = (
            fld.type
            if isinstance(fld, Dimension)
            else ("time" if isinstance(fld, TimeDimension) else "string")
        )
        try:
            f.validate_for_type(fld_type)
        except ValueError as exc:
            errors.append(
                ValidationError(
                    code="filter_type_mismatch",
                    message=str(exc),
                    cube=c.name,
                    field=fld.name,
                    op=f.op,
                    value=f.values[0] if f.values else None,
                )
            )

    filter_dim_refs = {f.dimension for f in query.filters}
    for c in touched:
        for req in c.required_filters:
            ref = f"{c.name}.{req}"
            if ref not in filter_dim_refs:
                errors.append(
                    ValidationError(
                        code="missing_required_filter",
                        message=(
                            f"Cube {c.name!r} requires a filter on {req!r}. "
                            f"Add Filter(dimension='{ref}', op=..., values=[...])."
                        ),
                        cube=c.name,
                        field=req,
                    )
                )

    if query.ungrouped:
        if query.limit is None:
            errors.append(
                ValidationError(
                    code="ungrouped_no_limit",
                    message=(
                        f"ungrouped=True queries must set a limit (maximum {MAX_UNGROUPED_ROWS})."
                    ),
                )
            )
        elif query.limit > MAX_UNGROUPED_ROWS:
            errors.append(
                ValidationError(
                    code="ungrouped_limit_too_high",
                    message=(
                        f"ungrouped=True requires limit <= {MAX_UNGROUPED_ROWS}. "
                        f"Got limit={query.limit}."
                    ),
                    value=query.limit,
                )
            )

    if query.offset is not None and query.offset > 0 and query.limit is None:
        errors.append(
            ValidationError(
                code="offset_without_limit",
                message=(
                    "SemanticQuery has offset set without limit. "
                    "OFFSET is only meaningful in combination with LIMIT."
                ),
                value=query.offset,
            )
        )

    query_measure_short_names: set[str] = set()
    for ref in query.measures:
        if "." in ref:
            query_measure_short_names.add(ref.rsplit(".", 1)[-1])
        else:
            query_measure_short_names.add(ref)
    for hf in query.having:
        short = hf.dimension.rsplit(".", 1)[-1] if "." in hf.dimension else hf.dimension
        if short not in query_measure_short_names:
            hint = (
                closest_match(short, query_measure_short_names)
                if query_measure_short_names
                else None
            )
            errors.append(
                ValidationError(
                    code="having_unknown_measure",
                    message=(
                        f"HAVING references {hf.dimension!r}, which is not a measure in this query."
                    ),
                    field=hf.dimension,
                    hint=hint,
                )
            )

    if query.compare is not None:
        errors.append(
            ValidationError(
                code="compare_unsupported",
                message=("compare windows are not yet supported by the compiler (Phase 2)."),
            )
        )

    backends = {c.backend for c in touched}
    if len(backends) > 1:
        names = sorted(b.value for b in backends)
        errors.append(
            ValidationError(
                code="cross_backend",
                message=(
                    "Cross-backend queries are not yet supported (Phase 2). "
                    f"Touched backends: {names}."
                ),
                extra={"backends": names},
            )
        )

    # Touch time_dim_field to silence unused warnings when we want it
    # for future shape checks. (Kept for symmetry with compile_query.)
    _ = time_dim_field

    return errors


__all__ = ["MAX_UNGROUPED_ROWS", "ValidationError", "validate"]
