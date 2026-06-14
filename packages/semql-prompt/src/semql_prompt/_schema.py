"""JSON-Schema post-processing shared by the tool-description projections.

Pydantic emits a *root* ``$ref`` when the root model is recursive — which
``SemanticQuery`` became once ``semi_joins[].source`` referenced
``SemanticQuery`` itself. A root ``$ref`` is not an object-rooted schema, and
OpenAI / Anthropic / Bedrock tool-calling all expect
``{"type": "object", "properties": {...}}`` at the top.

:func:`flatten_root_ref` splices the referenced definition up to the root
while keeping ``$defs`` and every *internal* (recursive) ``$ref`` intact, so
the self-reference still resolves. It is a no-op on an already object-rooted
schema.
"""

from __future__ import annotations

from typing import Any, cast


def _resolve_local_ref(schema: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local JSON-pointer ``$ref`` (RFC 6901) against ``schema``."""
    if not ref.startswith("#/"):
        raise ValueError(f"Only local '#/' JSON-pointer refs are supported; got {ref!r}.")
    node: Any = schema
    for raw in ref[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or token not in node:
            raise ValueError(f"Cannot resolve $ref {ref!r}: missing segment {token!r}.")
        node = cast("dict[str, Any]", node)[token]
    if not isinstance(node, dict):
        raise ValueError(f"$ref {ref!r} does not resolve to a schema object.")
    return cast("dict[str, Any]", node)


def flatten_root_ref(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` with a root ``$ref`` spliced inline to the top level.

    No-ops when the root is already object-rooted (no top-level ``$ref``).
    Keeps ``$defs`` so internal/recursive refs still resolve; any sibling keys
    alongside the root ``$ref`` take precedence over the spliced definition.
    Raises ``ValueError`` if the root ``$ref`` resolves to a non-object schema.
    """
    if "$ref" not in schema:
        return schema
    ref = schema["$ref"]
    if not isinstance(ref, str):
        raise ValueError(f"Root $ref must be a string; got {ref!r}.")
    target = _resolve_local_ref(schema, ref)
    if target.get("type") != "object":
        raise ValueError(
            f"Root $ref {ref!r} resolves to a non-object schema "
            f"(type={target.get('type')!r}); cannot flatten to an object root."
        )
    flattened: dict[str, Any] = dict(target)
    if "$defs" in schema:
        flattened["$defs"] = schema["$defs"]
    for key, value in schema.items():
        if key == "$ref":
            continue
        flattened[key] = value  # root siblings win over the spliced definition
    return flattened


__all__ = ["flatten_root_ref"]
