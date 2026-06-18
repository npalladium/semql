"""The single ``QualifiedRef`` type + canonical parser for ``"cube.field"``
references. One parsed type instead of ad-hoc ``.split(".")`` and a
shape check repeated at every use site."""

from __future__ import annotations

import pytest
from semql.errors import ResolveError
from semql.refs import (
    QualifiedRef,
    cube_of,
    field_of,
    is_qualified,
    local_name,
    parse_qualified_ref,
)


def test_parse_splits_cube_and_field() -> None:
    ref = parse_qualified_ref("orders.revenue")
    assert ref.cube == "orders"
    assert ref.field == "revenue"


def test_qualified_ref_is_a_str() -> None:
    # A str subclass, so it drops into any place a ref string is expected
    # (SemanticQuery.measures is list[str]) and equals its source text.
    ref = QualifiedRef("orders.revenue")
    assert isinstance(ref, str)
    assert ref == "orders.revenue"
    assert f"{ref}" == "orders.revenue"


def test_of_builds_from_halves() -> None:
    assert QualifiedRef.of("orders", "revenue") == "orders.revenue"


@pytest.mark.parametrize(
    "bad",
    ["revenue", "orders.", ".revenue", "a.b.c", "", "orders revenue", "1orders.x"],
)
def test_malformed_refs_raise_resolve_error(bad: str) -> None:
    with pytest.raises(ResolveError):
        parse_qualified_ref(bad)
    with pytest.raises(ResolveError):
        QualifiedRef(bad)


def test_error_message_names_the_offending_ref() -> None:
    with pytest.raises(ResolveError) as exc:
        parse_qualified_ref("not_qualified")
    assert "not_qualified" in str(exc.value)


def test_cube_of_and_field_of_are_strict() -> None:
    assert cube_of("orders.revenue") == "orders"
    assert field_of("orders.revenue") == "revenue"
    with pytest.raises(ResolveError):
        cube_of("bare")
    with pytest.raises(ResolveError):
        field_of("bare")


def test_is_qualified() -> None:
    assert is_qualified("orders.revenue") is True
    assert is_qualified("bare") is False
    assert is_qualified("a.b.c") is False


def test_local_name_is_tolerant() -> None:
    # Replaces ``ref.split(".",1)[1] if "." in ref else ref`` — field half
    # for a qualified ref, the whole string for a bare name (no raise).
    assert local_name("orders.revenue") == "revenue"
    assert local_name("revenue") == "revenue"


def test_qualified_ref_is_hashable_and_frozen() -> None:
    ref = QualifiedRef("orders.revenue")
    assert hash(ref) == hash("orders.revenue")
    assert {ref} == {"orders.revenue"}  # usable in a set / as a dict key
