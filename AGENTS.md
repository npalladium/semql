# SemQL — agent context

<!-- TODO: fill in once the core API is stable -->

## Layout

```
packages/semql/        # core: Cube, Catalog, SQL generation
packages/semql-mcp/    # MCP server
packages/semql-prompt/ # prompt builder
skills/                  # installable Claude Code skills
cubes/                   # (in user projects) cube definitions
```

## Commands

See `Justfile` — `just check` runs lint + typecheck + tests.
