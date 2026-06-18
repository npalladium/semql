"""Regression tests for the IntrospectMapper hardening (audit finding #2).

The OAuth introspection mapper now supports optional resource binding
(``audience`` / ``issuer``) and an opt-in ``scope_to_role`` allowlist so a
token minted elsewhere can't have its scopes silently become SemQL roles.
Defaults preserve the documented (scopes-as-roles-verbatim) behavior.
"""

from __future__ import annotations

from typing import Any

import pytest
from semql.errors import AuthError
from semql_auth import IntrospectMapper

_URL = "https://idp.example.com/oauth2/introspect"


class _FakeClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def post(
        self, url: str, *, data: dict[str, str], auth: tuple[str, str], timeout: float = 5.0
    ) -> dict[str, Any]:
        return self._response


def _mapper(response: dict[str, Any], **kwargs: Any) -> IntrospectMapper:
    return IntrospectMapper(
        _URL, client_id="c", client_secret="s", http_client=_FakeClient(response), **kwargs
    )


def test_scopes_become_roles_verbatim_by_default() -> None:
    """Default behavior is unchanged: scope tokens map straight to roles."""
    ctx = _mapper({"active": True, "sub": "alice", "scope": "read write admin"}).verify("t")
    assert ctx.roles == ["read", "write", "admin"]


def test_audience_mismatch_is_rejected() -> None:
    m = _mapper(
        {"active": True, "sub": "alice", "aud": "other-api", "scope": "admin"},
        audience="semql-api",
    )
    with pytest.raises(AuthError, match="audience"):
        m.verify("t")


def test_audience_match_in_list_is_accepted() -> None:
    ctx = _mapper(
        {"active": True, "sub": "alice", "aud": ["other-api", "semql-api"], "scope": "reader"},
        audience="semql-api",
    ).verify("t")
    assert ctx.viewer_id == "alice"


def test_issuer_mismatch_is_rejected() -> None:
    m = _mapper(
        {"active": True, "sub": "alice", "iss": "https://evil/", "scope": "admin"},
        issuer="https://idp.example.com/",
    )
    with pytest.raises(AuthError, match="issuer"):
        m.verify("t")


def test_scope_to_role_allowlist_translates_and_drops() -> None:
    """An unmapped scope (``admin``) can't leak through as a SemQL role."""
    ctx = _mapper(
        {"active": True, "sub": "alice", "scope": "read write admin"},
        scope_to_role={"read": "viewer", "write": "editor"},
    ).verify("t")
    assert ctx.roles == ["viewer", "editor"]


def test_scope_to_role_callable() -> None:
    def to_roles(scopes: list[str]) -> list[str]:
        return [f"role:{s}" for s in scopes]

    ctx = _mapper(
        {"active": True, "sub": "alice", "scope": "read write"},
        scope_to_role=to_roles,
    ).verify("t")
    assert ctx.roles == ["role:read", "role:write"]
