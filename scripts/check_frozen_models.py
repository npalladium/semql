#!/usr/bin/env python3
"""Enforce the value-object discipline: every Pydantic model is frozen.

SemQL's catalog and spec types are immutable value objects — frozen so they
hash, so a plan can't be mutated mid-compile, and so equality is by value.
This lint flags any Pydantic model that is neither frozen itself nor frozen
through a base class. A model that is *intentionally* not a frozen value
object (an abstract base, or an LLM-output / mutable builder) must be named
in ``FROZEN_EXEMPT`` — so the exception is explicit and reviewed, never silent.

Non-brittle by construction: it parses with :mod:`ast` (config set via
``ConfigDict(frozen=True)``, a ``{"frozen": True}`` dict, or a nested
``class Config`` are all recognised) and resolves frozen-ness across the
whole class hierarchy (a subclass of a frozen base counts as frozen), so it
matches how the models are actually written rather than a surface pattern.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Models exempt from the frozen requirement. Keep this short and justified:
# an abstract base that is never instantiated as a value object, or a model
# that is deliberately built up field-by-field (an LLM-output / mutable
# builder) rather than a frozen value object.
FROZEN_EXEMPT: frozenset[str] = frozenset(
    {
        # Abstract mixin: adds a value-based __hash__; it sets no config of
        # its own, and concrete subclasses declare frozen=True themselves.
        "_HashableModel",
    }
)

# Bases that make a subclass a Pydantic model even though we never see their
# own definition in source (they come from pydantic itself).
_MODEL_ROOTS = {"BaseModel"}


class _ClassInfo:
    __slots__ = ("name", "bases", "frozen_here", "file", "lineno", "is_test")

    def __init__(
        self, name: str, bases: list[str], frozen_here: bool, file: Path, lineno: int, is_test: bool
    ) -> None:
        self.name = name
        self.bases = bases
        self.frozen_here = frozen_here
        self.file = file
        self.lineno = lineno
        self.is_test = is_test


def _base_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _declares_frozen(cls: ast.ClassDef) -> bool:
    """Whether ``cls`` sets ``frozen=True`` in its own body."""
    for stmt in cls.body:
        # model_config = ConfigDict(frozen=True)  /  = {"frozen": True}
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets
        ):
            val = stmt.value
            if isinstance(val, ast.Call) and any(
                kw.arg == "frozen" and _is_true(kw.value) for kw in val.keywords
            ):
                return True
            if isinstance(val, ast.Dict):
                for key, value in zip(val.keys, val.values, strict=False):
                    if isinstance(key, ast.Constant) and key.value == "frozen" and _is_true(value):
                        return True
        # class Config: frozen = True
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            for inner in stmt.body:
                if (
                    isinstance(inner, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "frozen" for t in inner.targets)
                    and _is_true(inner.value)
                ):
                    return True
    return False


def collect_classes(roots: list[Path]) -> dict[str, _ClassInfo]:
    """Index every class defined under ``roots`` by name."""
    registry: dict[str, _ClassInfo] = {}
    for root in roots:
        for py in sorted(root.rglob("*.py")):
            is_test = "tests" in py.parts
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = [b for b in (_base_name(x) for x in node.bases) if b is not None]
                    registry[node.name] = _ClassInfo(
                        node.name, bases, _declares_frozen(node), py, node.lineno, is_test
                    )
    return registry


def _resolve(name: str, registry: dict[str, _ClassInfo], pred: str) -> bool:
    """Walk the base chain of ``name``; return True if ``pred`` holds for it
    or any ancestor. ``pred`` is 'model' (reaches a Pydantic root) or
    'frozen' (some class in the chain declares frozen)."""
    seen: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if pred == "model" and current in _MODEL_ROOTS:
            return True
        info = registry.get(current)
        if info is None:
            continue
        if pred == "frozen" and info.frozen_here:
            return True
        stack.extend(info.bases)
    return False


def find_violations(roots: list[Path]) -> list[_ClassInfo]:
    registry = collect_classes(roots)
    violations: list[_ClassInfo] = []
    for info in registry.values():
        if info.is_test or info.name in FROZEN_EXEMPT:
            continue
        if _resolve(info.name, registry, "model") and not _resolve(info.name, registry, "frozen"):
            violations.append(info)
    return sorted(violations, key=lambda i: (str(i.file), i.lineno))


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    roots = sorted((repo_root / "packages").glob("*/src"))
    violations = find_violations(roots)
    if violations:
        print(
            "These Pydantic models are not frozen (add frozen=True, inherit a "
            "frozen base, or add to FROZEN_EXEMPT if intentionally not a frozen "
            "value object):",
            file=sys.stderr,
        )
        for info in violations:
            print(
                f"  {info.file.relative_to(repo_root)}:{info.lineno}: {info.name}", file=sys.stderr
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
