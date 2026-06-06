"""The query spec the planner emits and the compiler consumes.

All identifiers are *qualified* — `cube.field`. The compiler resolves
them against the catalogue; unknown identifiers raise `CompileError`
naming the offending field.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, model_validator

FilterOp = Literal[
    "eq",
    "neq",
    "in",
    "not_in",
    "gt",
    "lt",
    "gte",
    "lte",
    "contains",
    "is_null",
    "not_null",
]


class TimeWindow(BaseModel):
    dimension: str
    granularity: Literal["hour", "day", "week", "month"] | None = None
    range: tuple[str, str]


class Filter(BaseModel):
    dimension: str
    op: FilterOp
    values: list[str | int | float | bool] = []

    def validate_for_type(self, dim_type: str) -> None:
        """Compile-time type check. Raises ValueError on mismatch; the
        compiler re-raises as CompileError."""
        if self.op in ("is_null", "not_null"):
            return
        if not self.values:
            raise ValueError(
                f"Filter on {self.dimension!r} with op={self.op!r} requires at least one value."
            )
        for v in self.values:
            if dim_type == "number":
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError(
                        f"Filter on numeric dimension {self.dimension!r} got "
                        f"non-numeric value {v!r}."
                    )
            elif dim_type == "bool":
                if not isinstance(v, bool):
                    raise ValueError(
                        f"Filter on bool dimension {self.dimension!r} got non-bool value {v!r}."
                    )
            elif dim_type == "time":
                if not isinstance(v, str):
                    raise ValueError(
                        f"Filter on time dimension {self.dimension!r} got non-string value {v!r}."
                    )
                try:
                    datetime.fromisoformat(v)
                except ValueError:
                    raise ValueError(
                        f"Filter on time dimension {self.dimension!r} got non-ISO-8601 value {v!r}."
                    ) from None
            elif dim_type == "string" and not isinstance(v, str):
                raise ValueError(
                    f"Filter on string dimension {self.dimension!r} got non-string value {v!r}."
                )


class CompareWindow(BaseModel):
    """`previous_period` derives the prior window from the TimeWindow's
    range (same duration, immediately prior). `explicit` requires `range`.
    Compare windows are not yet implemented in the compiler."""

    mode: Literal["previous_period", "explicit"] = "previous_period"
    range: tuple[str, str] | None = None


class SemanticQuery(BaseModel):
    measures: list[str] = []
    dimensions: list[str] = []
    time_dimension: TimeWindow | None = None
    filters: list[Filter] = []
    having: list[Filter] = []
    compare: CompareWindow | None = None
    order: list[tuple[str, Literal["asc", "desc"]]] = []
    limit: int | None = None
    # Row-listing mode (no GROUP BY). Incompatible with `measures`.
    ungrouped: bool = False

    @model_validator(mode="after")
    def _check_ungrouped_no_measures(self) -> SemanticQuery:
        if self.ungrouped and self.measures:
            raise ValueError(
                "ungrouped=True is incompatible with measures — measures "
                "imply aggregation, which ungrouped skips. Either drop "
                "the measures (row-listing mode) or set ungrouped=False "
                "(aggregated mode)."
            )
        return self


__all__ = [
    "CompareWindow",
    "Filter",
    "FilterOp",
    "SemanticQuery",
    "TimeWindow",
]
