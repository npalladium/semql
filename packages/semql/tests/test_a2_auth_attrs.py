"""A2 — AuthContext.attrs typed bag for arbitrary JWT claims.

Adds ``attrs: dict[str, Any]`` to AuthContext so callers can carry
structured claims (list values, booleans, numbers) without losing type
fidelity by going through the ``dict[str, str]`` metadata field.
"""

from __future__ import annotations

from semql.model import AuthContext


def test_attrs_defaults_to_empty_dict() -> None:
    ctx = AuthContext(viewer_id="u1")
    assert ctx.attrs == {}


def test_attrs_accepts_list_value() -> None:
    ctx = AuthContext(viewer_id="u1", attrs={"allowed_regions": ["west", "central"]})
    assert ctx.attrs["allowed_regions"] == ["west", "central"]


def test_attrs_accepts_bool_value() -> None:
    ctx = AuthContext(viewer_id="u1", attrs={"hr_clearance": True})
    assert ctx.attrs["hr_clearance"] is True


def test_attrs_accepts_nested_dict() -> None:
    ctx = AuthContext(viewer_id="u1", attrs={"claims": {"sub": "user@example.com"}})
    assert ctx.attrs["claims"]["sub"] == "user@example.com"


def test_attrs_survives_model_dump_round_trip() -> None:
    original = AuthContext(
        viewer_id="u1",
        roles=["admin"],
        attrs={"allowed_regions": ["west"], "level": 3},
    )
    dumped = original.model_dump()
    restored = AuthContext.model_validate(dumped)
    assert restored.attrs["allowed_regions"] == ["west"]
    assert restored.attrs["level"] == 3


def test_attrs_survives_model_copy() -> None:
    ctx = AuthContext(viewer_id="u1", attrs={"org_id": "acme"})
    copied = ctx.model_copy()
    assert copied.attrs["org_id"] == "acme"


def test_attrs_does_not_bleed_into_metadata() -> None:
    """attrs and metadata are independent fields."""
    ctx = AuthContext(
        viewer_id="u1",
        attrs={"a": 1},
        metadata={"m": "v"},
    )
    assert "a" not in ctx.metadata
    assert "m" not in ctx.attrs


def test_scope_fn_can_read_attrs() -> None:
    """A ScopeFn can access viewer.attrs to build a predicate."""
    from semql.model import ScopePredicate

    def _scope(viewer: AuthContext, cube: object) -> ScopePredicate | None:
        regions = viewer.attrs.get("allowed_regions", [])
        if not regions:
            return ScopePredicate(sql="1=0", ctx_keys=[])
        placeholders = ", ".join(f"%(r{i}s)s" for i in range(len(regions)))
        return ScopePredicate(sql=f"{{t}}.region IN ({placeholders})", ctx_keys=[])

    viewer = AuthContext(viewer_id="u1", attrs={"allowed_regions": ["west", "central"]})
    result = _scope(viewer, None)
    assert result is not None
    assert "west" not in result.sql  # SQL predicate, not literal value
