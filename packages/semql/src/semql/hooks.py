"""Extension hook Protocols for the semql compiler and prompt builder."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from semql.errors import CompileError
    from semql.model import Cube


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
