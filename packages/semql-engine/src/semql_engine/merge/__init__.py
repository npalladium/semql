"""Merge-SQL rendering for federated plans.

The core compiler (``semql.federate``) emits a structured
:class:`semql.federate.MergeSpec`; this subpackage turns it into the
DuckDB SQL the engine executes. Kept separate from the core so the
dialect knowledge lives beside the executor, not the planner.
"""

from __future__ import annotations

from semql_engine.merge.duckdb import render_merge_sql

__all__ = ["render_merge_sql"]
