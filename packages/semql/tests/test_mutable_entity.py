"""Tests for :class:`semql.MutableEntity` — the working form of :class:`Entity`.

``Entity`` is a frozen Pydantic value type — you can't mutate it after
construction. ``MutableEntity`` is the working form: build it up
incrementally via ``add_*`` / ``set_*`` / ``unset_*`` calls, then
``.freeze()`` to materialise a frozen :class:`Entity` for the catalog.

Use it when the entity shape is computed at runtime (e.g. an LLM-driven
config builder, a templated YAML loader, a migration script that adds
new entities to a catalog). The frozen form is the one that lives in
``Catalog.entities``; the mutable form never leaves the build phase.
"""

from __future__ import annotations

import pytest
from semql import Entity, MutableEntity


def _seed() -> MutableEntity:
    return MutableEntity.empty(name="user")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_empty_constructs_with_just_a_name() -> None:
    me = MutableEntity.empty(name="user")
    assert me.name == "user"
    assert me.cubes == []
    assert me.key is None
    assert me.fields == {}
    assert me.description == ""
    assert me.display_name is None


def test_from_entity_copies_all_fields() -> None:
    e = Entity(
        name="user",
        cubes=["users", "orders"],
        key="users.id",
        description="End user.",
        display_name="User",
        questions=["How many users?"],
        keywords=["user"],
        fields={"email": "users.email"},
        metadata={"owner": "growth"},
    )
    me = MutableEntity.from_entity(e)
    assert me.name == "user"
    assert me.cubes == ["users", "orders"]
    assert me.key == "users.id"
    assert me.description == "End user."
    assert me.display_name == "User"
    assert me.questions == ["How many users?"]
    assert me.keywords == ["user"]
    assert me.fields == {"email": "users.email"}
    assert me.metadata == {"owner": "growth"}


def test_from_entity_then_freeze_is_identity() -> None:
    """Round-trip Entity → MutableEntity → Entity preserves the value."""
    e = Entity(name="user", cubes=["users"], key="users.id", description="x")
    e2 = MutableEntity.from_entity(e).freeze()
    assert e2 == e


def test_freeze_returns_frozen_entity() -> None:
    me = MutableEntity.empty(name="user")
    e = me.add_cube("users").set_key("users.id").freeze()
    assert isinstance(e, Entity)
    assert e.cubes == ["users"]
    assert e.key == "users.id"
    # The frozen form is immutable.
    with pytest.raises(Exception):  # noqa: B017, BLE001
        e.name = "renamed"


def test_freeze_validates_at_boundary() -> None:
    """Validation happens when the working form is frozen — not at
    every mutation step. A builder may add cubes in any order; the
    freeze is the moment of truth."""
    me = MutableEntity.empty(name="orphan")
    # ``cubes=[]`` is invalid on Entity — freeze should refuse.
    with pytest.raises(ValueError, match=r"(?i)cubes|empty"):
        me.freeze()


# ---------------------------------------------------------------------------
# Cube management
# ---------------------------------------------------------------------------


def test_add_cube_appends() -> None:
    me = _seed().add_cube("users").add_cube("orders")
    assert me.cubes == ["users", "orders"]


def test_add_cube_is_idempotent() -> None:
    """Adding the same cube twice leaves the list unchanged (no duplicates)."""
    me = _seed().add_cube("users").add_cube("users")
    assert me.cubes == ["users"]


def test_add_cube_rejects_empty_string() -> None:
    with pytest.raises(ValueError, match=r"(?i)empty|cube"):
        _seed().add_cube("")


def test_remove_cube_drops_existing() -> None:
    me = _seed().add_cube("users").add_cube("orders").remove_cube("users")
    assert me.cubes == ["orders"]


def test_remove_cube_on_missing_is_silent() -> None:
    """Removing a cube the working form doesn't have is a no-op (caller
    may not know the full set when scripting). The freeze step is
    where the ``cubes=[]`` error fires."""
    me = _seed().add_cube("users").remove_cube("orders")
    assert me.cubes == ["users"]


# ---------------------------------------------------------------------------
# Key
# ---------------------------------------------------------------------------


def test_set_key() -> None:
    me = _seed().set_key("users.id")
    assert me.key == "users.id"


def test_set_key_replaces() -> None:
    me = _seed().set_key("users.id").set_key("users.email")
    assert me.key == "users.email"


def test_unset_key_clears() -> None:
    me = _seed().set_key("users.id").unset_key()
    assert me.key is None


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


def test_set_field_adds() -> None:
    me = _seed().set_field("email", "users.email")
    assert me.fields == {"email": "users.email"}


def test_set_field_replaces_existing() -> None:
    me = _seed().set_field("email", "users.email").set_field("email", "users.id")
    assert me.fields == {"email": "users.id"}


def test_unset_field_drops() -> None:
    me = _seed().set_field("email", "users.email").unset_field("email")
    assert me.fields == {}


def test_unset_field_on_missing_is_silent() -> None:
    me = _seed().unset_field("nope")
    assert me.fields == {}


# ---------------------------------------------------------------------------
# Description / display_name / metadata / questions / keywords
# ---------------------------------------------------------------------------


def test_set_description() -> None:
    me = _seed().set_description("End user of the product.")
    assert me.description == "End user of the product."


def test_set_display_name() -> None:
    me = _seed().set_display_name("User")
    assert me.display_name == "User"


def test_set_metadata_kv() -> None:
    me = _seed().set_metadata("owner", "growth")
    assert me.metadata == {"owner": "growth"}


def test_set_metadata_merges() -> None:
    me = _seed().set_metadata("owner", "growth").set_metadata("tier", "1")
    assert me.metadata == {"owner": "growth", "tier": "1"}


def test_unset_metadata_drops() -> None:
    me = _seed().set_metadata("owner", "growth").unset_metadata("owner")
    assert me.metadata == {}


def test_set_question_appends() -> None:
    me = _seed().set_question("How many users?").set_question("Where are they?")
    assert me.questions == ["How many users?", "Where are they?"]


def test_unset_question_removes_first_match() -> None:
    me = _seed().set_question("How many?").set_question("Where?").unset_question("How many?")
    assert me.questions == ["Where?"]


def test_set_keyword_appends_and_dedupes() -> None:
    me = _seed().set_keyword("user").set_keyword("user").set_keyword("account")
    assert me.keywords == ["user", "account"]


def test_unset_keyword_removes() -> None:
    me = _seed().set_keyword("user").set_keyword("account").unset_keyword("user")
    assert me.keywords == ["account"]


# ---------------------------------------------------------------------------
# Chaining semantics
# ---------------------------------------------------------------------------


def test_all_mutators_return_self_for_chaining() -> None:
    """Builder ergonomics: every mutation method returns ``self`` so
    a sequence reads as a pipeline."""
    me = (
        MutableEntity.empty(name="user")
        .add_cube("users")
        .set_key("users.id")
        .set_field("email", "users.email")
        .set_description("End user.")
        .set_metadata("owner", "growth")
    )
    e = me.freeze()
    assert e.cubes == ["users"]
    assert e.key == "users.id"
    assert e.fields == {"email": "users.email"}
    assert e.description == "End user."
    assert e.metadata == {"owner": "growth"}


def test_mutable_entity_is_not_frozen() -> None:
    """The point of the type: it's mutable, unlike Entity."""
    me = _seed()
    me.cubes.append("users")  # direct attribute mutation works
    assert me.cubes == ["users"]
