"""Bedrock Converse tool-schema adaptation.

Bedrock's Converse API validates every tool ``inputSchema`` and requires the
TOP LEVEL to be an object schema carrying ``type: "object"``. A bare root
``$ref`` is rejected on every model family — Nova, Claude, Llama, Mistral,
Qwen, and gpt-oss all return the identical
``inputSchema.json.type must be one of the following: object`` error (verified
empirically against the live Converse API, 2026-06). The constraint lives in
the Converse tool layer, above the model, so it applies regardless of which
model you call.

Pydantic emits a root ``$ref`` whenever the *root* model is recursive (e.g. a
self-referential :class:`~semql.spec.BoolExpr`). Internal ``$ref`` / ``$defs``
— including recursive cycles — are accepted fine, so this module rewrites ONLY
the root and leaves the rest of the schema (nested refs, ``$defs``,
``prefixItems`` tuples) untouched. Fully inlining ``$defs`` would be both
unnecessary and impossible for a genuine recursive cycle.

:meth:`SemanticQuery.model_json_schema() <semql.spec.SemanticQuery>` is already
object-rooted, so :func:`flatten_root_ref` is a no-op for it; the rewrite bites
only when a recursive model is exported directly as a tool root, and the guard
keeps that from regressing into a silently-rejected schema.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, cast

from semql_prompt.catalog_tools import to_openai_tools

if TYPE_CHECKING:
    from semql import Catalog
    from semql.model import AuthContext


def flatten_root_ref(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``schema`` whose top level is an object schema.

    If the root is a ``$ref`` into ``$defs`` (Pydantic's output for a recursive
    root model), splice the referenced definition's body up to the top level —
    keeping ``$defs`` intact so internal/recursive ``$ref``\\ s still resolve.
    Sibling keywords sitting next to the root ``$ref`` (``description``,
    ``default``, …) are preserved and win over the spliced body. If the root is
    already an object schema, it is returned unchanged (deep-copied).

    Raises:
        ValueError: if the schema cannot be made object-rooted — e.g. a
            ``RootModel`` over a union or scalar whose top level is ``anyOf`` or
            a non-object ``type``. Bedrock requires an object root, so such a
            model cannot be a Converse tool input; fail loudly rather than ship
            a schema the API will reject at request time.
    """
    schema = copy.deepcopy(schema)
    ref = schema.get("$ref")
    if ref is not None:
        defs = schema.get("$defs")
        target = _resolve_local_ref(ref, schema)
        # Keywords beside the root $ref override the spliced body (JSON Schema
        # 2020-12 allows $ref siblings; Pydantic uses them for default/title).
        siblings = {k: v for k, v in schema.items() if k not in ("$ref", "$defs")}
        schema = {**target, **siblings}
        if defs is not None:
            # Retain $defs untouched — the spliced body (and any recursive
            # node) still references entries inside it.
            schema["$defs"] = defs
    if schema.get("type") != "object":
        raise ValueError(
            "flatten_root_ref: schema is not object-rooted after flattening "
            f"(top-level type={schema.get('type')!r}, keys={sorted(schema)}). "
            "Bedrock Converse requires a tool inputSchema whose root is "
            "type='object'; a RootModel over a union or scalar cannot be a "
            "Converse tool input."
        )
    return schema


def _resolve_local_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local JSON-pointer ``$ref`` (e.g. ``#/$defs/Name``) in ``root``."""
    if not ref.startswith("#/"):
        raise ValueError(f"flatten_root_ref: only local '#/...' refs are supported, got {ref!r}.")
    node: Any = root
    for raw in ref[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")  # RFC 6901 unescape
        try:
            node = node[token]
        except (KeyError, TypeError):
            raise ValueError(f"flatten_root_ref: cannot resolve ref {ref!r}.") from None
    if not isinstance(node, dict):
        raise ValueError(f"flatten_root_ref: ref {ref!r} does not point at a schema object.")
    return cast("dict[str, Any]", node)


def to_bedrock_converse_tools(
    catalog: Catalog,
    *,
    viewer: AuthContext | None = None,
    only_exposed: bool = True,
) -> list[dict[str, Any]]:
    """One Bedrock Converse ``toolSpec`` per visible cube + saved query.

    Returns dicts shaped for ``toolConfig["tools"]`` in
    ``bedrock-runtime.converse(...)``::

        {"toolSpec": {"name": ..., "description": ...,
                      "inputSchema": {"json": <object-rooted schema>}}}

    Built by reshaping :func:`~semql_prompt.catalog_tools.to_openai_tools` — so
    cube / saved-query visibility, role gating, and descriptions stay defined in
    one place — and running each parameter schema through
    :func:`flatten_root_ref` for Converse root-``$ref`` compatibility.
    """
    tools: list[dict[str, Any]] = []
    for tool in to_openai_tools(catalog, viewer=viewer, only_exposed=only_exposed):
        fn = tool["function"]
        tools.append(
            {
                "toolSpec": {
                    "name": fn["name"],
                    "description": fn["description"],
                    "inputSchema": {"json": flatten_root_ref(fn["parameters"])},
                }
            }
        )
    return tools
