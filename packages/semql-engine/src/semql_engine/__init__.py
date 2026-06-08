"""Public surface of semql-engine."""

from __future__ import annotations

from semql_engine.adapter import (
    Adapter,
    AdapterResult,
    AsyncAdapter,
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

__all__ = [
    "Adapter",
    "AdapterResult",
    "AsyncAdapter",
    "AsyncDuckDBAdapter",
    "AsyncEngine",
    "AsyncMergeEngine",
    "DBAPIAdapter",
    "DuckDBMergeEngine",
    "DuckDBAdapter",
    "Engine",
    "EngineError",
    "ExecutionResult",
    "MergeEngine",
    "to_async_adapter",
    "to_async_merge_engine",
]
