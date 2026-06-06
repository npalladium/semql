# SemQL

Semantic data layer with SQL generation and MCP exposure.

## What it does

Define **semantic cubes** — dimensions, measures, filters, and joins over your tables.
Organize them in a **catalog**. SemQL handles the rest.

- **SQL generation** — turn a semantic spec into dialect-aware SQL
- **MCP server** — exposes `query_semantic(spec)` plus auto-generated
  `query_<cube>()` tools so any MCP client can query your catalog
- **Prompt builder** — generates a system-prompt fragment that teaches an LLM
  your catalog schema so it routes queries through the semantic layer

## Packages

| Package | Description |
|---|---|
| `semql` | Core: cube definitions, catalog, SQL generation |
| `semql-mcp` | MCP server wrapping a catalog |
| `semql-prompt` | Prompt builder for LLM integration |

## Install

```sh
pip install semql
pip install semql-mcp     # + MCP server
pip install semql-prompt  # + prompt builder
```

## Quick start

```python
from semql import Cube, Catalog

orders = Cube(
    "orders",
    table="orders",
    dimensions=["region", "product"],
    measures={"revenue": "sum(amount)", "orders": "count(*)"},
)

catalog = Catalog([orders])
sql = catalog.query(cube="orders", dims=["region"], measures=["revenue"])
```

## Status

Early development. Contributions welcome.
