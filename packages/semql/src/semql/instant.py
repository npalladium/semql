"""Time-instant parsing.

``parse_instant`` is a pure utility that the time-partitioned source
routing (``semql.partition``) and the catalog model
(``semql.model.TimePartitionedSource``) both need, and the catalog
model also flows into the spec tree (``semql.spec``). To keep the
import graph a clean DAG the function lives here as a leaf: any
module that needs ISO-8601 instant parsing imports it directly,
without dragging the spec / model dependency chain.

Lives at the top of the dep graph: no semql imports.
"""

from __future__ import annotations

from datetime import UTC, datetime


def parse_instant(value: str) -> datetime:
    """Parse an ISO-8601 time-range endpoint to an aware UTC ``datetime``.

    Range routing is about *instants*, not bytes. ``"2024-01-01"``,
    ``"2024-01-01T00:00:00"`` and ``"2024-01-01T05:00:00+05:00"`` all
    denote the same moment and must compare equal — lexical string
    comparison gets this wrong the instant two endpoints carry different
    UTC offsets or differing precision (the A2 routing bug). Naive
    timestamps are read as UTC so a naive endpoint stays comparable with
    an offset-bearing one; per-cube timezone semantics are tracked
    separately (architecture review B9). Raises ``ValueError`` naming the
    offending value if it is not valid ISO-8601.

    >>> parse_instant("2024-01-01")
    datetime.datetime(2024, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
    """
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not a valid ISO-8601 datetime: {value!r}") from exc
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


__all__ = ["parse_instant"]
