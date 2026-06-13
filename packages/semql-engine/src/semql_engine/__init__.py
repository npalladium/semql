"""Public surface of semql-engine."""

from __future__ import annotations

from semql_engine.adapter import (
    Adapter,
    AdapterResult,
    AsyncAdapter,
    AsyncBigQueryAdapter,
    AsyncClickHouseAdapter,
    AsyncDBAPIAdapter,
    AsyncDuckDBAdapter,
    DBAPIAdapter,
    DuckDBAdapter,
    to_async_adapter,
)
from semql_engine.engine import (
    AsyncEngine,
    AsyncMergeEngine,
    DuckDBMergeEngine,
    Engine,
    EngineError,
    ExecutionResult,
    MergeEngine,
    to_async_merge_engine,
)
from semql_engine.rows import (
    InMemoryRowAdapter,
    RowCapableAdapter,
    execute_entity,
)

__all__ = [
    "Adapter",
    "AdapterResult",
    "AsyncAdapter",
    "AsyncBigQueryAdapter",
    "AsyncClickHouseAdapter",
    "AsyncDBAPIAdapter",
    "AsyncDuckDBAdapter",
    "AsyncEngine",
    "AsyncMergeEngine",
    "DBAPIAdapter",
    "DuckDBMergeEngine",
    "DuckDBAdapter",
    "Engine",
    "EngineError",
    "ExecutionResult",
    "InMemoryRowAdapter",
    "MergeEngine",
    "RowCapableAdapter",
    "execute_entity",
    "to_async_adapter",
    "to_async_merge_engine",
]
