"""Tests for :class:`semql.Entity` — the business-object counterpart to ``Cube``.

An entity names a business object ("User", "Order", "LeaveInstance") that
maps onto one or more catalog cubes. The cube is the SQL surface; the
entity is the prompt / LLM / product surface. Entities do NOT influence
the compiler — they're descriptive metadata with construction-time
validation that the referenced cubes (and the entity's field / key
references) actually exist.

Two practical uses today:

1. **Prompt fragments.** A planner rendering a long catalog benefits from
   "what is this object?" summaries that read as business vocabulary
   rather than "table X with columns a, b, c". An entity carries the
   vocabulary.

2. **Composite / LogicalEntity shape.** A business object that spans
   multiple physical cubes (``User = UserInfo + Identity``) declares
   them on one entity; downstream tools iterate ``entity.cubes`` to
   know which cubes to materialise.

This file covers the model layer only — Catalog wiring lives in
``test_entities_catalog.py``.
"""

from __future__ import annotations

import pytest
from semql import Entity

# ---------------------------------------------------------------------------
# Model — Entity shape
# ---------------------------------------------------------------------------


def test_entity_constructs_with_name_and_cubes() -> None:
    e = Entity(name="user", cubes=["users"])
    assert e.name == "user"
    assert e.cubes == ["users"]
    assert e.key is None
    assert e.fields == {}


def test_entity_constructs_composite_across_two_cubes() -> None:
    """A 'LogicalEntity' shape: the entity spans multiple cubes."""
    e = Entity(
        name="user_full",
        cubes=["users", "orders"],
        key="users.id",
    )
    assert e.cubes == ["users", "orders"]
    assert e.key == "users.id"


def test_entity_preserves_prompt_metadata() -> None:
    e = Entity(
        name="user",
        cubes=["users"],
        description="An end user of the product.",
        display_name="User",
        questions=["How many users signed up this month?"],
        keywords=["user", "customer", "account"],
        metadata={"owner": "growth"},
    )
    assert e.description == "An end user of the product."
    assert e.display_name == "User"
    assert e.questions == ["How many users signed up this month?"]
    assert e.keywords == ["user", "customer", "account"]
    assert e.metadata == {"owner": "growth"}


def test_entity_with_field_renames() -> None:
    """Like View, an entity may rename cube fields under its own scope."""
    e = Entity(
        name="checkout_user",
        cubes=["users"],
        fields={
            "id": "users.id",
            "email": "users.email",
        },
    )
    assert e.fields == {"id": "users.id", "email": "users.email"}


def test_entity_is_frozen() -> None:
    """Entity is a value type; mutation must be impossible."""
    e = Entity(name="user", cubes=["users"])
    with pytest.raises(Exception):  # noqa: B017, BLE001 — Pydantic raises ValidationError
        e.name = "renamed"


def test_entity_rejects_empty_cubes() -> None:
    """An entity that names no cube is meaningless — refuse at construction."""
    with pytest.raises(ValueError, match=r"(?i)entity|cubes|empty"):
        Entity(name="orphan", cubes=[])


def test_entity_rejects_duplicate_cube_names() -> None:
    """Same cube twice would double-count; refuse."""
    with pytest.raises(ValueError, match=r"(?i)entity|cubes|duplicate|unique"):
        Entity(name="dup", cubes=["users", "users"])


def test_entity_rejects_unqualified_field_target() -> None:
    with pytest.raises(ValueError, match=r"(?i)qualified|cube\.field"):
        Entity(
            name="bad",
            cubes=["users"],
            fields={"x": "no_dot"},
        )


def test_entity_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match=r"(?i)entity|name|empty"):
        Entity(name="", cubes=["users"])


def test_entity_kind_is_entity() -> None:
    """Structural tag, like BaseField.kind. Lets consumers branch on type
    without importing the class."""
    e = Entity(name="user", cubes=["users"])
    assert e.kind == "entity"


def test_entity_key_must_be_qualified() -> None:
    """``key`` references a dim on a cube: must be ``cube.dim`` form."""
    with pytest.raises(ValueError, match=r"(?i)key|qualified|cube\.dim"):
        Entity(name="bad", cubes=["users"], key="id")


# ---------------------------------------------------------------------------
# Model — Entity equality + immutability semantics
# ---------------------------------------------------------------------------


def test_entity_structural_equality() -> None:
    """Two Entity models with the same fields compare equal — value-type semantics."""
    a = Entity(name="user", cubes=["users"], description="d")
    b = Entity(name="user", cubes=["users"], description="d")
    assert a == b
    assert a is not b  # distinct objects, equal value


def test_entity_copy_with_replacement() -> None:
    """``model_copy(update=...)`` is the canonical mutation path for
    frozen models — verify it works on Entity (Pydantic inherited
    behaviour, asserted so the contract doesn't silently regress)."""
    e = Entity(name="user", cubes=["users"])
    e2 = e.model_copy(update={"description": "An end user."})
    assert e.description == ""
    assert e2.description == "An end user."
    assert e2.cubes == ["users"]
