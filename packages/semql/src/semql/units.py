"""Unit conversion registry for measure / dimension display.

A measure declares the unit its column *stores* (``unit="seconds"``)
and, optionally, the unit it should be *shown* in
(``display_unit="hours"``). The compiler ignores both. Downstream
presenters call :func:`convert` to translate raw row values into the
display unit.

Borrowed from the ``unum`` library: keep the storage value honest,
declare the display unit separately, raise loudly when a conversion
is undefined instead of silently mis-rendering.

Three modes for renderers:

1. ``display_unit`` set         → convert literally to that unit.
2. ``display_unit`` unset,
   ``format="duration"``        → :func:`auto_pick` chooses the
                                   largest time unit where the value
                                   is ≥ 1 (90s → 1.5 min).
3. ``display_unit`` unset       → render raw in the storage unit.

Built-in vocabulary:

* Time   — ``microseconds``, ``milliseconds``, ``seconds``,
  ``minutes``, ``hours``, ``days``, ``weeks``.
* Bytes  — ``bytes``, ``kb``, ``mb``, ``gb``, ``tb`` (decimal,
  1000-step; SI / disk vendor convention). Users wanting binary
  (1024-step) can register ``kib`` / ``mib`` / ``gib`` themselves.

Aliases: ``s``/``sec``/``secs`` → ``seconds``; ``ms`` →
``milliseconds``; ``us``/``µs`` → ``microseconds``; ``min``/``mins``
→ ``minutes``; ``hr``/``hrs`` → ``hours``; ``day`` → ``days``;
``wk``/``wks`` → ``weeks``; ``b``/``byte`` → ``bytes``.

Lookup is case-insensitive. ``UnknownUnit`` fires if a unit name
isn't registered; ``IncompatibleUnits`` fires if both sides are
known but live in disconnected components (e.g. ``seconds`` →
``bytes``).

Escape hatch — three layers:

* Process-wide:    :func:`register` / :func:`register_alias` on the
  default registry.
* Isolated:        construct your own :class:`Registry` and pass it
  through (e.g. ``Catalog(unit_registry=...)``).
* One-off:         do the math yourself; the library never *forces*
  callers through the registry.
"""

from __future__ import annotations

from collections import deque

# Public symbols organised at the bottom of the file.


class UnitError(ValueError):
    """Base class for unit-conversion failures."""


class UnknownUnit(UnitError):
    """Raised when a unit name isn't registered."""


class IncompatibleUnits(UnitError):
    """Raised when two known units have no conversion path between them."""


class Registry:
    """An isolated unit-conversion graph.

    The default process-wide registry is :data:`DEFAULT_REGISTRY`; the
    module-level functions (``convert`` / ``factor`` / ``register`` /
    ``register_alias`` / ``auto_pick`` / ``known_units``) delegate to
    it. Construct your own ``Registry`` for tenant- or catalog-scoped
    overrides — ``Catalog(unit_registry=Registry()).unit_registry`` is
    independent of the global one.

    Aliases collapse at lookup time, not at registration time, so an
    alias can be added before its target exists.
    """

    def __init__(self) -> None:
        # ``_graph[canonical_unit] = {neighbour_canonical_unit: factor}``.
        # ``factor`` is the multiplier from key to neighbour:
        # ``_graph["seconds"]["minutes"] = 1/60``  ⇒  120s × 1/60 = 2min.
        self._graph: dict[str, dict[str, float]] = {}
        # ``_aliases[alias] = canonical``. Both sides stored lowercased.
        self._aliases: dict[str, str] = {}

    # -- Normalisation ------------------------------------------------------

    def canonical(self, unit: str) -> str:
        """Return the canonical form of ``unit`` — alias-resolved and
        lowercased. Does NOT check membership in the graph."""
        key = unit.strip().lower()
        return self._aliases.get(key, key)

    # -- Public API ---------------------------------------------------------

    def register(self, from_unit: str, to_unit: str, factor: float) -> None:
        """Declare ``1 from_unit == factor × to_unit``.

        Registers the inverse edge automatically. Transitive paths
        compose at :meth:`convert` time via BFS, so registering each
        adjacent pair is enough to convert across multiple hops.
        """
        if factor == 0:
            raise ValueError(f"conversion factor must be non-zero (got {factor})")
        a = self.canonical(from_unit)
        b = self.canonical(to_unit)
        if a == b:
            raise ValueError(f"cannot register a self-edge for {a!r}")
        self._graph.setdefault(a, {})[b] = factor
        self._graph.setdefault(b, {})[a] = 1.0 / factor

    def register_alias(self, alias: str, canonical: str) -> None:
        """Register ``alias`` as a synonym for ``canonical``.

        The canonical name doesn't have to exist in the graph yet —
        useful for shipping aliases alongside built-in registrations
        without ordering them carefully.
        """
        a = alias.strip().lower()
        c = canonical.strip().lower()
        if a == c:
            return
        self._aliases[a] = c

    def known_units(self) -> frozenset[str]:
        """Return the set of canonical unit names registered in this graph."""
        return frozenset(self._graph)

    def _path_factor(self, src: str, dst: str) -> float:
        """BFS the unit graph; return the cumulative factor from
        ``src`` to ``dst``. Raises ``IncompatibleUnits`` if no path
        exists.
        """
        if src == dst:
            return 1.0
        visited: dict[str, float] = {src: 1.0}
        queue: deque[str] = deque([src])
        while queue:
            node = queue.popleft()
            cum = visited[node]
            for nb, edge in self._graph[node].items():
                if nb in visited:
                    continue
                visited[nb] = cum * edge
                if nb == dst:
                    return visited[nb]
                queue.append(nb)
        raise IncompatibleUnits(f"no conversion path from {src!r} to {dst!r}")

    def convert(self, value: float, from_unit: str, to_unit: str) -> float:
        """Convert ``value`` from ``from_unit`` to ``to_unit``.

        Raises ``UnknownUnit`` if either side isn't registered, or
        ``IncompatibleUnits`` if both are known but unreachable.
        """
        a = self.canonical(from_unit)
        b = self.canonical(to_unit)
        if a not in self._graph:
            raise UnknownUnit(f"unknown unit: {from_unit!r}")
        if b not in self._graph:
            raise UnknownUnit(f"unknown unit: {to_unit!r}")
        return value * self._path_factor(a, b)

    def factor(self, from_unit: str, to_unit: str) -> float:
        """Return the multiplicative factor mapping ``from_unit`` to
        ``to_unit``. Convenience for renderers that want to bake the
        factor into SQL or apply it column-wise to a numpy array.
        """
        return self.convert(1.0, from_unit, to_unit)

    def auto_pick(
        self,
        value: float,
        from_unit: str,
        candidates: list[str],
    ) -> tuple[float, str]:
        """Pick the largest candidate where ``|converted_value| >= 1``.

        ``candidates`` must be ordered from smallest to largest unit.
        If every candidate yields a value below 1 (or the input is
        zero), the smallest candidate wins — so "0 seconds" displays
        in the finest grain rather than degenerating.

        Returns ``(converted_value, picked_unit)``. The picked unit is
        the canonical name; callers wanting a custom label render it
        themselves.

        Use this for ``format="duration"`` style adaptive picking
        when ``display_unit`` is not set. Example:

        >>> r = Registry()
        >>> # ...defaults loaded...
        >>> r.auto_pick(5400, "seconds", ["seconds", "minutes", "hours", "days"])
        (1.5, 'hours')
        """
        if not candidates:
            raise ValueError("candidates list is empty")
        best_value = self.convert(value, from_unit, candidates[0])
        best_unit = candidates[0]
        for cand in candidates[1:]:
            cval = self.convert(value, from_unit, cand)
            if abs(cval) >= 1.0:
                best_value, best_unit = cval, cand
            else:
                break
        return best_value, best_unit


# ---------------------------------------------------------------------------
# Default process-wide registry + module-level convenience wrappers
# ---------------------------------------------------------------------------


def _build_default() -> Registry:
    r = Registry()

    # Time — base: seconds. Decimal step where applicable; calendar
    # units stop at "weeks" because months / years aren't a static
    # factor (variable day counts).
    r.register("seconds", "minutes", 1.0 / 60.0)
    r.register("minutes", "hours", 1.0 / 60.0)
    r.register("hours", "days", 1.0 / 24.0)
    r.register("days", "weeks", 1.0 / 7.0)
    r.register("seconds", "milliseconds", 1000.0)
    r.register("milliseconds", "microseconds", 1000.0)

    # Bytes — decimal, 1000-step (SI / disk vendor convention).
    # Binary (KiB / MiB / GiB, 1024-step) intentionally NOT registered
    # — meaning is context-dependent (RAM vs disk vs network), so
    # users opt in explicitly with ``register("kib", "mib", 1/1024)``.
    r.register("bytes", "kb", 1.0 / 1000.0)
    r.register("kb", "mb", 1.0 / 1000.0)
    r.register("mb", "gb", 1.0 / 1000.0)
    r.register("gb", "tb", 1.0 / 1000.0)

    # Aliases — short forms that appear in real-world catalogues.
    for alias, canonical in (
        ("s", "seconds"),
        ("sec", "seconds"),
        ("secs", "seconds"),
        ("ms", "milliseconds"),
        ("us", "microseconds"),
        ("µs", "microseconds"),
        ("min", "minutes"),
        ("mins", "minutes"),
        ("hr", "hours"),
        ("hrs", "hours"),
        ("day", "days"),
        ("wk", "weeks"),
        ("wks", "weeks"),
        ("b", "bytes"),
        ("byte", "bytes"),
    ):
        r.register_alias(alias, canonical)
    return r


DEFAULT_REGISTRY: Registry = _build_default()


# Canonical candidate lists for ``auto_pick``. Callers can pass their
# own; these cover the common cases.
TIME_AUTOPICK: tuple[str, ...] = (
    "microseconds",
    "milliseconds",
    "seconds",
    "minutes",
    "hours",
    "days",
)
BYTES_AUTOPICK: tuple[str, ...] = ("bytes", "kb", "mb", "gb", "tb")


def register(from_unit: str, to_unit: str, factor: float) -> None:
    """Declare a conversion on the default registry. See :meth:`Registry.register`."""
    DEFAULT_REGISTRY.register(from_unit, to_unit, factor)


def register_alias(alias: str, canonical: str) -> None:
    """Declare an alias on the default registry. See :meth:`Registry.register_alias`."""
    DEFAULT_REGISTRY.register_alias(alias, canonical)


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert via the default registry. See :meth:`Registry.convert`."""
    return DEFAULT_REGISTRY.convert(value, from_unit, to_unit)


def factor(from_unit: str, to_unit: str) -> float:
    """Factor via the default registry. See :meth:`Registry.factor`."""
    return DEFAULT_REGISTRY.factor(from_unit, to_unit)


def auto_pick(
    value: float,
    from_unit: str,
    candidates: list[str] | None = None,
) -> tuple[float, str]:
    """Auto-pick via the default registry. See :meth:`Registry.auto_pick`.

    If ``candidates`` is None, picks a default list based on the
    canonical form of ``from_unit`` — time units get
    :data:`TIME_AUTOPICK`, byte units get :data:`BYTES_AUTOPICK`,
    anything else raises ``ValueError`` and forces the caller to be
    explicit.
    """
    if candidates is None:
        canonical = DEFAULT_REGISTRY.canonical(from_unit)
        if canonical in TIME_AUTOPICK:
            candidates = list(TIME_AUTOPICK)
        elif canonical in BYTES_AUTOPICK:
            candidates = list(BYTES_AUTOPICK)
        else:
            raise ValueError(
                f"auto_pick has no default candidate list for {from_unit!r}; "
                "pass `candidates=` explicitly."
            )
    return DEFAULT_REGISTRY.auto_pick(value, from_unit, candidates)


def known_units() -> frozenset[str]:
    """Known units in the default registry. See :meth:`Registry.known_units`."""
    return DEFAULT_REGISTRY.known_units()


__all__ = [
    "BYTES_AUTOPICK",
    "DEFAULT_REGISTRY",
    "IncompatibleUnits",
    "Registry",
    "TIME_AUTOPICK",
    "UnitError",
    "UnknownUnit",
    "auto_pick",
    "convert",
    "factor",
    "known_units",
    "register",
    "register_alias",
]
