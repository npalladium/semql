# LLM-facing interface — friendliness review (2026-06)

Status: proposal. Covers both consumption paths: the **chatbot path**
(catalog → prompt fragments → LLM constructs `SemanticQuery`) and the
**MCP path** (`semql-mcp` tools). Companion to
`explain-cost-planner-2026-06.md` (the `ExplainReport` and
`CostEstimate` types referenced below are specified there).

---

## 0. What is already right

The foundations are unusually good; the review below is about
envelopes, not architecture.

- Per-cube MCP tools with `Literal`-constrained measure/dimension
  names (`server.py:412`) — the schema itself prevents hallucinated
  fields.
- Did-you-mean hints on `UnknownIdentifierError` via `closest_match`
  (`_resolve.py:52`, `errors.py:158`).
- `validate` tool with **collect-all** structured records — the LLM
  sees every problem in one round-trip.
- `catalog_prompt` with S7 retrieval narrowing, role-gated overlay
  segments, and `PromptBudget` trimming.
- `resolve_lookup` / `list_lookup_values` for free-text → canonical
  value resolution.
- Query aliases (I14) and the result `kind` tag (I15), shipped
  2026-06-08.

---

## 1. The error envelope drops structure we already have

**Highest-yield fix in this doc.** `UnknownIdentifierError` carries
`kind`, `name`, `cube`, `hint`; `PlaceholderError` carries `known`.
`_error_payload` (`server.py:369`) flattens all of it to
`{code, message}` — the machine-readable repair data survives only as
prose inside `message`. LLMs repair far better from structured
alternatives than from prose.

```python
{"error": {
    "code": "UnknownIdentifierError",
    "message": "Unknown field 'revnue' on cube 'orders'. ...",
    "kind": "field",
    "name": "revnue",
    "cube": "orders",
    "hint": "revenue",
    "valid_alternatives": ["revenue", "order_count", ...],
    "next_tool": null,
}}
```

Mechanics: a `to_payload()` on `SemQLError` that subclasses extend —
the taxonomy already has the fields; this is serialization only.

Two extensions while in there:

- **`next_tool` affordance.** When the repair is a tool call, name
  it: a filter-value miss on a `Lookup`-backed dimension should say
  `{"next_tool": "resolve_lookup", "args": {"dimension":
  "orders.status", "query": "shiped"}}` — or better, run the lookup
  internally and return `"did_you_mean": ["shipped"]` directly.
  Today `FilterTypeError` carries no suggestions at all; the Lookup
  machinery to generate them already exists.
- **Collect-all on the query path.** `validate` collects all errors;
  the `query_*` tools fail on the first. Option: on compile failure,
  run collect-all validation and return the full record list in the
  error envelope — one round-trip instead of N repair cycles.

## 2. Errors masquerading as success

MCP `explain` returns `"-- compile failed: {exc}"` as its success
payload (`server.py:145–162`). An LLM cannot distinguish that from
SQL. Return the §1 error envelope. Same review for any tool that
stringifies an exception into its normal return channel.

## 3. Expose cost and the explain report

`estimate_cost` / `QueryBudget` / `CostEstimate` are not reachable via
MCP at all.

- Attach `cost` to every `query_*` / `query_semantic` response — it is
  computed from data already in hand, pre-compile, essentially free.
- Replace the SQL-string `explain` tool with the structured
  `ExplainReport` (sibling doc §1): plan summary + decisions + SQL +
  cost. This enables the genuinely good agent behaviour: *"this scans
  ~2B rows — narrow the time window before I run it?"* — a refusal
  the agent can negotiate instead of a timeout it can't.
- When a `QueryBudget` is configured server-side, `BudgetExceededError`
  flows through the §1 envelope with the estimate attached, so the
  LLM knows *how far over* it is and what to shrink.

## 4. Few-shot the catalog prompt

`render_catalog_block` renders example *questions* per cube; LLMs
imitate **(question → SemanticQuery JSON)** pairs far better than
schema prose. `SavedQuery` objects are ready-made few-shots —
(name, spec, description) — render the top few per catalog into
`build_planner_prompt_fragment` for free. For cubes without saved
queries, one hand-authored pair per cube (a `Cube.examples` field or
the existing `cube_prompt_hooks`) beats another paragraph of contract
text. Few-shots participate in `PromptBudget` trimming like
descriptions do.

## 5. Result-side token budget

`query_execute` returns unbounded rows into the agent's context
window. `PromptBudget` protects the catalog side; the result side
needs its sibling:

- `max_rows` cap (server-configured default + per-call override),
- truncation marker + `total_count` so the LLM knows it saw a prefix,
- optionally `format: "json" | "markdown"` — markdown tables are
  materially cheaper in tokens for wide results.

Without this, one fat query blows the context and the agent loses the
whole conversation, not just the query.

## 6. Tool-count scaling

Per-cube tools are the right call below ~30–50 cubes; beyond that they
blow the client's tool budget (and many MCP clients degrade sharply
past a few dozen tools). The S7 retrieval threshold already encodes
this judgement for prompts — apply the same threshold to tools:

- below threshold: per-cube `query_<cube>` tools (today's behaviour);
- above: collapse to `find_cubes(question) → query_semantic` two-step,
  with `find_cubes` backed by the existing `Retriever` protocol.

One threshold, two consumers, consistent behaviour.

## 7. Small wires

- **`readOnlyHint` annotations** on `query_semantic`, `validate`,
  `explain`, `catalog_prompt`, `list_lookup_values`, `resolve_lookup`,
  and the per-cube tools (FastMCP supports tool annotations). Lets
  harnesses parallelize calls and skip confirmation prompts.
  `query_execute` stays unannotated (it touches a database).
- **Schema version in outputs.** A `semql_version` / catalog-schema
  version in `catalog_prompt` and tool responses, so long-lived agents
  detect drift across server restarts. ("Wire formats versioned.")
- **Tool descriptions teach the repair loop.** Per-cube docstrings
  should mention `validate` ("unsure? validate first — it returns all
  problems at once") and `resolve_lookup` where lookups exist. The
  description is the only documentation the LLM reliably reads.
- **Curate the JSON schema.** `SemanticQuery` field descriptions
  exist; add `json_schema_extra` examples on the hairy fields
  (`where` trees, `compare`, `order` tuples) — examples in the schema
  ride along to every function-calling consumer, including
  `to_openai_function`.

---

## 8. Sequencing

None of this depends on B1. Ordered by yield-per-effort:

| item                                   | effort | slot              |
|----------------------------------------|--------|-------------------|
| §1 structured error envelope           | S      | Week 1 (with W1)  |
| §2 kill `-- compile failed` string     | XS     | Week 1            |
| §3 cost in query responses             | S      | Week 1            |
| §5 result-side row cap                 | S      | Week 1–2          |
| §7 readOnlyHint + docstring repair loop| XS     | Week 1–2          |
| §4 saved-query few-shots               | M      | W6 (DX)           |
| §1 `next_tool` / lookup-aware filters  | M      | W6                |
| §6 find_cubes two-step                 | M      | W8 (demand-gated*)|
| §3 ExplainReport via MCP               | M      | after sibling §1  |

\* §6 is demand-gated honestly: it only matters for catalogs past the
threshold; build it when one exists. The threshold *check* + a warning
log when exceeded can ship now (no silent caps).
