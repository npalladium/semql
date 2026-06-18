"""``DerivedTable.sql`` must reject a *top-level* nested ``WITH``.

The compiler hoists every cube's CTEs into one outer ``WITH``, so a
``DerivedTable`` whose ``sql`` is itself ``WITH ... SELECT ...`` is refused
at construction with a migration hint pointing at the explicit ``with_ctes``
field; a ``WITH`` nested inside a subquery is fine (it is self-contained,
never hoisted)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from semql.model import DerivedTable, NamedCTE


def test_rejects_top_level_with() -> None:
    with pytest.raises(ValidationError) as exc:
        DerivedTable(sql="WITH c AS (SELECT 1 AS x) SELECT x FROM c")
    msg = str(exc.value)
    assert "with_ctes" in msg
    assert "WITH" in msg


def test_rejects_top_level_with_on_union() -> None:
    with pytest.raises(ValidationError):
        DerivedTable(sql="WITH c AS (SELECT 1 AS x) SELECT x FROM c UNION ALL SELECT 2")


def test_allows_with_nested_in_subquery() -> None:
    # The inner WITH is self-contained inside the derived subquery; it is
    # never hoisted, so it does not collide with sibling cubes' CTEs.
    DerivedTable(sql="SELECT * FROM (WITH c AS (SELECT 1 AS x) SELECT x FROM c) s")


def test_allows_plain_select() -> None:
    DerivedTable(sql="SELECT a, b FROM t WHERE x > 1")


def test_allows_explicit_with_ctes_form() -> None:
    # The supported form: the main sql references the CTE by bare name.
    DerivedTable(
        sql="SELECT * FROM my_cte",
        with_ctes=[NamedCTE(name="my_cte", sql="SELECT 1 AS id")],
    )


def test_unparseable_sql_does_not_crash_construction() -> None:
    # Catalog SQL is trusted; a fragment we can't parse dialect-lessly must
    # not block construction — the compiler surfaces real errors later.
    DerivedTable(sql="SELECT {placeholder} FROM t")
