# semql-prompt

Status: **stub**.

The core prompt-rendering primitives — `render_catalogue_block`,
`build_planner_prompt_fragment`, `build_router_prompt_fragment` —
already live in [`semql`](../semql/README.md) (`semql.prompt`). They
need nothing beyond `semql` itself to produce a planner system-prompt
fragment from a `Catalog`.

This package exists as a stake for the next layer: an opinionated
prompt / planner composer that turns a natural-language question into
a `SemanticQuery` by calling an LLM. The shape we're planning:

```python
from semql_prompt import Planner
from anthropic import Anthropic

planner = Planner(catalog=my_catalog, client=Anthropic())
spec = planner.plan("revenue by region for the last 30 days")
# returns a SemanticQuery the compiler accepts
```

The decision still open (tracked in the repo TODO):
1. **Re-export + CLI**: `python -m semql_prompt render` over the
   existing `semql.prompt` helpers, no extra deps.
2. **LLM-calling layer**: bring in `anthropic` (or pluggable client)
   so the package actually composes a planner.

Until the scope settles, **use `semql.prompt` directly** for prompt
rendering:

```python
from semql import Catalog
catalog: Catalog = ...
print(catalog.prompt())                       # planner fragment
print(catalog.prompt(include_introspection=True))
```

## Install

```sh
pip install semql-prompt   # currently equivalent to ``pip install semql``
```

## Status

Pre-v1, stub. Don't depend on this package's import surface yet —
when the scope decision lands, the shape will change.
