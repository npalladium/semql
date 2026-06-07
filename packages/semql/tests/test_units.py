"""Tests for :mod:`semql.units`.

Three things the registry has to get right:

* Direct edges convert correctly (and the inverse is auto-derived).
* Transitive paths compose via BFS — registering each adjacent pair
  is enough to convert any-to-any inside a connected component.
* Failures surface as ``UnknownUnit`` / ``IncompatibleUnits`` instead
  of returning a plausible-looking wrong number.

We exercise an isolated :class:`Registry` for behavioural tests so the
module-level :data:`DEFAULT_REGISTRY` stays untouched between cases.
"""

from __future__ import annotations

import math

import pytest
from semql.units import (
    DEFAULT_REGISTRY,
    TIME_AUTOPICK,
    IncompatibleUnits,
    Registry,
    UnknownUnit,
    auto_pick,
    convert,
    factor,
    known_units,
    register,
    register_alias,
)


def _close(a: float, b: float, rel: float = 1e-9, absolute: float = 1e-12) -> bool:
    """Local stand-in for ``pytest.approx`` — pyright can't fully type
    ``pytest.approx``, so we use ``math.isclose`` to keep tests
    typechecker-clean. Defaults are tight enough to catch real bugs and
    loose enough to absorb BFS-path multiplication noise."""
    return math.isclose(a, b, rel_tol=rel, abs_tol=absolute)


# ---------------------------------------------------------------------------
# Direct conversions on the default registry
# ---------------------------------------------------------------------------


def test_default_registry_seconds_to_hours() -> None:
    assert _close(convert(3600, "seconds", "hours"), 1.0)


def test_default_registry_inverse_is_auto_registered() -> None:
    """register() also writes the inverse edge so the user doesn't
    repeat themselves."""
    assert _close(convert(2, "hours", "seconds"), 7200.0)


def test_default_registry_transitive_seconds_to_days() -> None:
    """No direct edge from seconds → days; BFS composes via minutes/hours."""
    assert _close(convert(86_400, "seconds", "days"), 1.0)


def test_default_registry_milliseconds_to_minutes() -> None:
    assert _close(convert(60_000, "ms", "min"), 1.0)


def test_default_registry_microseconds_canonicalised() -> None:
    assert _close(convert(1_000_000, "us", "seconds"), 1.0)
    assert _close(convert(1_000_000, "µs", "seconds"), 1.0)


def test_factor_is_convert_of_one() -> None:
    assert _close(factor("seconds", "minutes"), convert(1.0, "seconds", "minutes"))


# ---------------------------------------------------------------------------
# Bytes — decimal (1000-step) by default
# ---------------------------------------------------------------------------


def test_default_bytes_use_decimal_step() -> None:
    """Defaults match SI / disk-vendor convention (KB = 1000 B), not
    binary. Users who want 1024-step register their own ``kib`` etc."""
    assert _close(convert(1000, "bytes", "kb"), 1.0)
    assert _close(convert(1_000_000, "bytes", "mb"), 1.0)
    assert _close(convert(2_500_000_000, "bytes", "gb"), 2.5)


def test_byte_aliases_work() -> None:
    assert _close(convert(2048, "b", "kb"), 2.048)
    assert _close(convert(1, "byte", "bytes"), 1.0)


# ---------------------------------------------------------------------------
# Aliases — case-insensitive, short-form acceptance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("s", "seconds"),
        ("Sec", "seconds"),
        ("SECS ", "seconds"),
        ("ms", "milliseconds"),
        ("hr", "hours"),
        ("Hrs", "hours"),
        ("min", "minutes"),
        ("MINS", "minutes"),
        ("wk", "weeks"),
    ],
)
def test_alias_round_trips_to_self(alias: str, canonical: str) -> None:
    assert DEFAULT_REGISTRY.canonical(alias) == canonical


def test_aliases_convert_identically_to_canonical() -> None:
    """Aliasing must not change the numeric answer."""
    assert convert(120, "s", "min") == convert(120, "seconds", "minutes")


# ---------------------------------------------------------------------------
# Failure surface — fast and loud, never silent
# ---------------------------------------------------------------------------


def test_unknown_unit_raises_unknown_unit() -> None:
    with pytest.raises(UnknownUnit, match="furlongs"):
        convert(1, "furlongs", "seconds")


def test_incompatible_units_raise_incompatible_units() -> None:
    """Both sides registered, but in disconnected components — must
    raise rather than silently composing them."""
    with pytest.raises(IncompatibleUnits):
        convert(1, "seconds", "bytes")


def test_unknown_unit_is_unit_error_subclass() -> None:
    """Callers can catch ``UnitError`` to handle either failure mode."""
    from semql.units import UnitError

    with pytest.raises(UnitError):
        convert(1, "wibble", "seconds")


def test_zero_factor_rejected() -> None:
    r = Registry()
    with pytest.raises(ValueError, match="non-zero"):
        r.register("a", "b", 0.0)


def test_self_edge_rejected() -> None:
    r = Registry()
    with pytest.raises(ValueError, match="self-edge"):
        r.register("seconds", "seconds", 1.0)


# ---------------------------------------------------------------------------
# Isolated Registry — escape hatch for tenant / catalog-scoped overrides
# ---------------------------------------------------------------------------


def test_isolated_registry_does_not_touch_default() -> None:
    """Building a fresh Registry must NOT mutate the default."""
    before = known_units()
    r = Registry()
    r.register("widgets", "gadgets", 5.0)
    assert "widgets" not in known_units()  # default untouched
    assert known_units() == before
    assert _close(r.convert(2, "widgets", "gadgets"), 10.0)


def test_isolated_registry_can_override_byte_step() -> None:
    """A tenant wanting binary bytes can build a registry with
    1024-step edges without affecting the shared one."""
    r = Registry()
    r.register("bytes", "kib", 1.0 / 1024.0)
    r.register("kib", "mib", 1.0 / 1024.0)
    assert _close(r.convert(1024, "bytes", "kib"), 1.0)
    assert _close(r.convert(1024 * 1024, "bytes", "mib"), 1.0)


# ---------------------------------------------------------------------------
# auto_pick — adaptive "duration" mode
# ---------------------------------------------------------------------------


def test_auto_pick_chooses_largest_unit_where_value_ge_one() -> None:
    """90 seconds reads better as 1.5 min than 0.025 hours or 90 s."""
    value, unit = auto_pick(90, "seconds")
    assert unit == "minutes"
    assert _close(value, 1.5)


def test_auto_pick_hits_hours_for_large_durations() -> None:
    value, unit = auto_pick(5400, "seconds")
    assert unit == "hours"
    assert _close(value, 1.5)


def test_auto_pick_falls_back_to_smallest_when_value_below_threshold() -> None:
    """Input too small for even the smallest candidate to satisfy
    ``|val| >= 1`` — degenerate to the smallest grain rather than
    overshoot. ``0.1 µs`` stays as microseconds (0.1), not as
    milliseconds (0.0001)."""
    value, unit = auto_pick(0.0001, "milliseconds")
    assert unit == "microseconds"
    assert _close(value, 0.1)


def test_auto_pick_picks_intermediate_unit_when_appropriate() -> None:
    """500ms input — milliseconds (500) satisfies >=1, seconds (0.5)
    doesn't. Pick milliseconds."""
    value, unit = auto_pick(0.5, "seconds")
    assert unit == "milliseconds"
    assert _close(value, 500.0)


def test_auto_pick_zero_value_picks_finest_grain() -> None:
    """A zero never satisfies >= 1; we want a sensible default rather
    than NaN. Smallest candidate wins."""
    value, unit = auto_pick(0, "seconds", candidates=list(TIME_AUTOPICK))
    assert unit == TIME_AUTOPICK[0]
    assert _close(value, 0.0)


def test_auto_pick_respects_explicit_candidates() -> None:
    """Caller can constrain to a subset (e.g. don't show microseconds in a UI)."""
    value, unit = auto_pick(0.001, "seconds", candidates=["seconds", "minutes"])
    assert unit == "seconds"
    assert _close(value, 0.001)


def test_auto_pick_bytes_default_list() -> None:
    value, unit = auto_pick(2_500_000_000, "bytes")
    assert unit == "gb"
    assert _close(value, 2.5)


def test_auto_pick_raises_for_unknown_dimension() -> None:
    """No default candidate list for non-time / non-bytes units —
    caller must opt in explicitly."""
    with pytest.raises(ValueError, match="candidates"):
        auto_pick(1, "currency")


def test_auto_pick_empty_candidates_rejected() -> None:
    r = Registry()
    r.register("a", "b", 2.0)
    with pytest.raises(ValueError, match="empty"):
        r.auto_pick(1.0, "a", [])


# ---------------------------------------------------------------------------
# Module-level register() / register_alias() — exercise the side effects
# safely by registering then converting through aliases that don't collide
# with the built-ins.
# ---------------------------------------------------------------------------


def test_module_register_and_alias_extend_default() -> None:
    """Smoke-test the module-level helpers. Use vocabulary that won't
    collide with built-ins so the default registry stays usable for
    subsequent tests in this run."""
    register("widgets", "megawidgets", 1.0 / 1_000_000.0)
    register_alias("wdg", "widgets")
    assert _close(convert(2_000_000, "wdg", "megawidgets"), 2.0)


# ---------------------------------------------------------------------------
# Floating-point sanity — short paths don't drift
# ---------------------------------------------------------------------------


def test_short_path_round_trip_is_exact_enough() -> None:
    """Convert there-and-back across a 3-hop path; should land within
    floating-point noise of the original. Guards against accidental
    factor inversion bugs in BFS."""
    seconds = 86_400.0
    days = convert(seconds, "seconds", "days")
    back = convert(days, "days", "seconds")
    assert math.isclose(back, seconds, rel_tol=1e-12)
