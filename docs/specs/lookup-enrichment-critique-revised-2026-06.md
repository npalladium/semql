# Critique: SemQL's lookup / enrichment architecture (revised)

A three-lens review of how `Lookup`, `enrich_result`, `Join`, `Segment`,
`InlineDerived`, `CompareWindow`, `drill_paths`, and the
`Presentation` / `Drilldown` types answer the question "given a query
result, how does the user get the information they actually want?"

This is a **revised** critique. The first pass
(`lookup-enrichment-critique-2026-06.md`) misread the design by
treating the LLM as just another caller of a query API. The actual
user story is more specific and changes several of the conclusions:

> SemQL builds text-to-SQL chatbots. The LLM is the planner. The LLM
> emits `SemanticQuery(dimensions=["orders.region_id"])` when the
> catalog author meant for the user to see "EMEA" not "12". The
> `Lookup` / `enrich_result` machinery is the **defensive layer that
> attaches the human label whether the LLM remembered to ask for it
> or not**. The catalog declares *intent* ("this dimension has a
> label"); the runtime enforces *presentation* ("the result row
> always carries the label, regardless of what the LLM wrote in the
> spec").

The chatbot surfaces are **three** of them, not two:

1. **MCP** — the LLM calls `query_execute` over JSON-RPC, gets rows
   back. The defensive guarantee has to be enforced inside the MCP
   server (`packages/semql-mcp/src/semql_mcp/server.py`).
2. **Direct Python API** — the chatbot author uses Pydantic-AI,
   Anthropic SDK, OpenAI Agents, or a custom harness. They call
   `compile_query(...)` + their own executor directly, and have to
   invoke `enrich_result` themselves. The defensive guarantee has to
   be reachable as a one-liner in the Python API.
3. **Pydantic-AI recipe package** — a planned `semql-pydantic-ai`
   sibling package (S1 in the roadmap scratchpad
   `bj3fkv8a1.txt:436`: *"Thin recipe package: `make_planner_agent`,
   `make_router_agent`, etc., returning pydantic_ai.Agent instances
   wired to prompt fragments + output models. Optional, opinionated.
   Closes integration gap for largest agent framework without
   forcing it on core."*) that wraps the core primitives for the
   largest agent framework.

> In a typical Pydantic-AI chatbot, the flow is: a `@tool` function
> takes a `SemanticQuery` from the LLM, calls
> `compile_query(q, catalog)`, runs the SQL against a database
> driver, and returns the rows. The defensive layer is "after
> running the SQL, walk the rows and attach the labels." The chatbot
> author is responsible for *invoking* this layer unless we wire it
> into a helper they call instead of `compile_query` directly — and
> "we" is the recipe package, not the core.

That single fact reframes several critiques:

- The "free function the caller has to remember to call" framing
  (first critique §3) is right in spirit but wrong in target. The
  *Python-API caller* (chatbot author) is a person, and the free
  function is fine for them *if* it has good discoverability. The
  *MCP caller* (LLM over JSON-RPC) is not a person and cannot
  "remember" anything — the server must enforce. The *Pydantic-AI
  caller* is also a person but is typically building a tool layer
  that wraps `compile_query` + executor; the recipe package should
  give them a helper that does both in one call.
- The "compiler could do the join instead" alternatives (A, C, H in
  the first critique) are also wrong. The whole point is that the
  *LLM's* query gets enriched. A compile-time join would require the
  LLM to write the join, which is exactly the thing the design is
  trying to not depend on.
- "Why is this a runtime step at all?" — because the join can't
  happen at compile time without changing the SQL the LLM wrote, and
  changing the LLM's SQL is exactly the failure mode the design
  exists to prevent.

### The library, not framework, distinction is the load-bearing one

`PHILOSOPHY.md:96` — *"Not a framework. It composes with your stack
— it does not own it."* The project has already decided that
*opinionated* integrations live in sibling packages, not in core.
The roadmap scratchpad calls these "thin recipe packages" and lists
several: `semql-pydantic-ai` (S1), `semql codegen` (S2), `semql`
dbt/SQLMesh ingest (S6). Each is "Optional, opinionated" — i.e.
framework-shaped — and each lives outside core.

This re-rates the alternatives. Some of my suggestions were
*framework-shaped* and would push core toward owning a call site
it should leave to the caller. The library principle says: **core
ships primitives; recipes wire them up.** Where the defensive
guarantee lives depends on which tier owns the call site.

- The MCP server is *already* a recipe-shaped surface (it owns the
  JSON-RPC boundary and the tool registration). The MCP package is
  where "use SemQL over MCP" is opinionated. So J (MCP `query_execute`
  calls `enrich_result` by default) is the *right* place to make
  the opinionated choice. It does not push core toward framework;
  it pushes the recipe toward correct behaviour.
- The planner prompt lives in `semql-prompt`, a separate package
  (see `AGENTS.md` repository layout). It is a recipe: it ships
  prompt-fragment builders, not the prompts themselves. So K
  (planner-prompt mentions the guarantee) is also a recipe change,
  not a core change. The recipe gets the one-line note; the core
  Lookup model is unchanged.
- The Pydantic-AI / Anthropic / custom-harness chatbot loop is
  *not* core's job. A `compile_and_run` helper in `semql/__init__.py`
  would push core toward framework — it would own the executor
  signature, the result-enrichment contract, and the caller's
  intent to "just give me labelled rows." The PHILOSOPHY invariant
  *"The compiler has no I/O. ... running the SQL is the caller's
  job."* (`PHILOSOPHY.md:16-18`) is bent by that helper. The right
  place for `compile_and_run` is the recipe package
  (`semql-pydantic-ai` or a sibling `semql-chatbot`), not core.

This revision reverses my earlier call on alternative J2. The
principle "core is a library" *and* the principle "running the SQL
is the caller's job" *and* the existing roadmap commitment to ship
recipe packages as siblings all say the same thing: the Python-API
chatbot helper belongs in a recipe, not in core. The MCP fix stays
in MCP; the Pydantic-AI fix goes in the recipe.

With that context, the architecture is **more load-bearing than the
first critique gave it credit for**, and several of the "consolidate
into one mechanism" suggestions dissolve. The remaining critique is
about (a) the *plumbing* of the guarantee through the recipe-shaped
surfaces (MCP, planner-prompt, the planned Pydantic-AI recipe), (b)
the parts of the design that *don't* serve the text-to-SQL story
and can be revisited, and (c) the model layer's internal coherence.

The review covers model layer (`model.py`, `spec.py`, `plan.py`),
compiler layer (`compile.py`), runtime helpers (`lookups.py`),
discovery (`introspect.py`), and the MCP surface (`server.py`).

## Overview

The architecture is well-shaped for its purpose. The "enrich" word
being reserved for the post-query id→label column is the right
distinction: it separates "what the LLM is allowed to do" (a SQL
join, declared in the catalog) from "what the user is guaranteed to
see" (a label column, attached at runtime). The
`enrich_result`-via-`LookupEnricher` split, with a `loader` callable
that can be either a simple vocabulary list or a batch id→label
resolver, is the right shape for the actual constraint: the catalog
author has the data, the LLM has the SQL, and neither has the label
column the user wants.

What weakens the design, in light of the real user story:

1. **The MCP server doesn't honour the guarantee.** `query_execute`
   in `packages/semql-mcp/src/semql_mcp/server.py:226-261` returns
   raw rows. Every LLM-driven chatbot built on the MCP server is
   shipping `region: "12"` to users. The `enrich_result` helper
   exists in the public Python API (`semql.enrich_result`) but the
   LLM is the consumer, not the Python caller. This is the single
   biggest gap between the design intent and the shipped behaviour.

   **Tier of fix:** recipe (MCP). The MCP server is *already* the
   opinionated integration point — it owns the JSON-RPC boundary,
   the tool registration, the executor. Adding `enrich_result` to
   its `query_execute` is the right place to make the opinionated
   choice. It does not push core toward framework; it pushes the
   recipe toward correct behaviour.

2. **The Python API exposes the helper but no built-in path uses
   it.** `semql.enrich_result` is a free function. A chatbot author
   using Pydantic-AI, Anthropic SDK, OpenAI Agents, or a custom
   harness calls `compile_query(q, catalog)` directly, runs the SQL
   themselves, and has to remember to call `enrich_result` for
   every dimension with a `Lookup`. The chatbot author is a person
   and the function is fine, but there's no
   `compile_and_execute(query, catalog, executor, ctx=...)` helper
   that does compile + run + enrich in one call. Every Pydantic-AI
   chatbot in the wild is re-inventing the same loop, and most are
   forgetting the enrich step. (The
   `demos/pipeline_demo.py` demo stubs out the executor entirely —
   it does not call `enrich_result` either.)

   **Tier of fix:** recipe (`semql-pydantic-ai` / `semql-chatbot`).
   Not core. The PHILOSOPHY invariant *"The compiler has no I/O.
   ... running the SQL is the caller's job."* (`PHILOSOPHY.md:16-18`)
   is core's job to enforce; the helper that bundles
   compile-execute-enrich belongs to the recipe that owns the
   caller's loop. The existing roadmap already plans
   `semql-pydantic-ai` as a sibling recipe package (S1 in the
   roadmap scratchpad); the `compile_and_run` helper is the
   *natural* content for that recipe.

3. **The LLM never sees the guarantee.** The planner prompt renders
   `Lookup.values` as a finite vocabulary — "EMEA is a valid value
   of `region`." It does *not* tell the LLM "you don't need to ask
   for the label, it's attached for you." The LLM, not knowing the
   guarantee, either asks for the label itself (waste) or doesn't
   (the gap above). The defensive layer is invisible to the thing
   it's defending against.

   **Tier of fix:** recipe (`semql-prompt`). The planner prompt
   already lives in a sibling package (per `AGENTS.md` repository
   layout). The one-line addition to the prompt is a recipe
   change, not a core change. The `Lookup` model in core is
   unchanged.

4. **The model has accreted mechanisms that don't serve the
   text-to-SQL story.** `InlineDerived`, `CompareWindow`,
   `drill_paths`, `Presentation`/`DrilldownSuggestion` — each has a
   rationale, but the rationale isn't "the LLM might forget to ask
   for this and the user will suffer." The `Lookup` / `enrich_result`
   story is *defensive* (guarantees the label). The other mechanisms
   are *expressive* (lets the LLM ask for things the catalog doesn't
   pre-declare). Different design pressures, mixed in the same
   model.

   **Tier of fix:** core. The expressive mechanisms are part of
   the *model*; the recipe surface is irrelevant here. The critique
   is that they don't earn their keep for the text-to-SQL story and
   should probably be cut down — but this is a separate document.

**The single most important thing to fix:** make the
`Lookup`/`enrich_result` guarantee *work* in the recipes that own
each chatbot surface. The MCP server (a recipe) calls
`enrich_result` by default in `query_execute`. The
`semql-prompt` recipe adds the one-line note to the planner. The
`semql-pydantic-ai` recipe ships the `compile_and_run` helper. Core
is unchanged.

---

## Structural feedback

### 1. The defensive-vs-expressive split is the real axis the model is missing

The model layer has **six distinct field types that can affect a
result set**, and the docstring of none of them references the others:

- `Measure` — emits an aggregation column
- `Dimension` — emits a GROUP BY column
- `TimeDimension` — emits a time bucket
- `Segment` — emits a WHERE predicate
- `Join` — emits a SQL JOIN
- `Lookup` — emits a *runtime-attached* `<dim>__label` column

But there are really two *axes*:

- **Expressive** (`Measure`, `Dimension`, `TimeDimension`, `Segment`,
  `Join`, `InlineDerived`, `CompareWindow`) — what the LLM is *allowed*
  to ask for.
- **Defensive** (`Lookup`, `enrich_result`, `aliases`, default
  `SemanticQueryDefaults`) — what the user is *guaranteed* to see
  regardless of what the LLM asked for.

`Lookup` is the only mechanism in the model that is purely defensive.
Every other mechanism is expressive. That's a feature, not a bug —
the design is consistent on this axis. But the *docstrings* of the
expressive mechanisms (`InlineDerived`, `CompareWindow`) talk about
"the exploratory shape an LLM or human reaches for in chat" — i.e.
they admit the LLM is the caller — while the `Lookup` docstring talks
about the catalog author's vocabulary. The reader is told the LLM is
a first-class caller for the expressive mechanisms and a second-class
caller for the defensive ones. That mismatch is what makes the design
feel fragmented.

**Suggestion:** One sentence in `PHILOSOPHY.md`: *"Some mechanisms
help the LLM ask for things; one mechanism (`Lookup`) guarantees the
user sees things the LLM didn't ask for."* Then the docstring of
`Lookup` can drop its "I/O boundary" framing and talk about the
defensive guarantee, and the docstring of `InlineDerived` can
continue to talk about LLM-driven exploration.

### 2. The MCP server doesn't honour the defensive guarantee

This is the headline finding. The LLM-driven text-to-SQL story the
design exists to serve is broken at the MCP boundary.

In `packages/semql-mcp/src/semql_mcp/server.py:226-261`,
`query_execute` returns the rows from the executor untouched. The
`enrich_result` helper is not called. The `Lookup` declarations on
the catalog are silently ignored at result time.

The result: an LLM calling `query_execute` over MCP gets
`{region: "12", revenue: 12345}` and ships that to the user, even
though the catalog author declared `Lookup(dimension="orders.region",
values={"12": "EMEA"})`. The user sees the ID; the label the
catalog author intended them to see is in another process.

This is exactly the failure mode `Lookup`/`enrich_result` is designed
to prevent. The Python API exposes the fix
(`semql.enrich_result(rows, "region", lookup, ctx)`) but the LLM
consumer doesn't have access to the Python API — the MCP server is
its only handle.

**Suggestion (this is alternative J from the first critique, now
*recovered* as the right call):** `query_execute` should call
`enrich_result` automatically for any dimension in the result that
has a registered `Lookup`. The return type grows an
`enrichment: dict[str, str]` map (which dimension was enriched, and
how) so the consumer can opt out per call (`enrich=False`).

The cost is small (one closure call, conditional on a registered
Lookup). The benefit is that the design *works as advertised* for
the LLM consumer it's meant to serve. The current behaviour is
"design intent, not implemented."

### 2b. The Python API has the helper but no path that uses it

The MCP server is one of the chatbot surfaces. The other is
**direct Python API use** — the chatbot author writes a tool layer
in Pydantic-AI, Anthropic SDK, OpenAI Agents, or a custom harness
that calls `semql.compile_query(...)` and runs the SQL against a
database driver. This is the surface the design's `Lookup` /
`enrich_result` story is *primarily* aimed at: a person writing a
chatbot in Python, who can be told "after you run the SQL, call
`enrich_result`."

But the function is a free function in the public API
(`semql.enrich_result`) and the catalog and the
`ResolutionContext` are not bundled with it. The chatbot author's
loop is:

```python
compiled = catalog.compile_query(q, viewer=viewer, context=ctx)
rows = my_db_driver.execute(compiled.sql, compiled.params)
for dim_name, lookup in catalog.lookups.items():
    if dim_name in compiled.columns:  # heuristic
        rows = enrich_result(rows, dim_name, lookup, ResolutionContext(...))
```

Three places to get it wrong:

- Forgetting the loop entirely (the most common failure mode).
- Iterating over `catalog.lookups` instead of over the dimensions
  that actually appear in the result (waste, and may crash on
  enrichers that can't handle a particular dimension).
- Building the `ResolutionContext` wrong (the enricher may need a
  viewer, may need context, may need both).

**Suggestion (and a reversal from the first pass):** do *not* add
`compile_and_run` to `semql/__init__.py`. The PHILOSOPHY invariant
*"The compiler has no I/O. ... running the SQL is the caller's
job."* (`PHILOSOPHY.md:16-18`) is core's job to enforce. A
`compile_and_run` helper in core would own the executor signature,
the result-enrichment contract, and the caller's intent to "just
give me labelled rows" — i.e. it would push core toward framework.
That's the wrong direction.

**The right place for `compile_and_run` is the recipe package** —
the planned `semql-pydantic-ai` (S1 in the roadmap) or a sibling
`semql-chatbot` recipe. The recipe is *allowed* to be opinionated
about the call site; that's its job. The recipe ships
`make_planner_agent`, `make_router_agent`, *and* `make_query_tool`
— where `make_query_tool` is the helper that bundles
compile-execute-enrich for Pydantic-AI. Core is unchanged.

> The roadmap scratchpad (S1) calls this package *"Thin recipe
> package: `make_planner_agent(catalog, viewer, model=...)`,
> `make_router_agent(...)`, etc., returning pydantic_ai.Agent
> instances wired to prompt fragments + output models. Optional,
> opinionated."* The `compile_and_run` helper is the natural next
> entry in that list — not in core.

The cost: the recipe is one more package. The benefit: core stays a
library, the call site is owned by a recipe, and the PHILOSOPHY
invariant isn't bent.

### 3. The planner prompt doesn't tell the LLM the guarantee exists

Symmetrically, the LLM doesn't know that labels are attached for
free. The `Lookup` is rendered into the prompt as a vocabulary list
("`orders.region` has values: EMEA, APAC, NA"). The LLM reads this
and concludes it can ask for `region` and the value will be a
recognisable string. What it doesn't know:

- The label is *attached* to the result row automatically, even if
  it asks for `region` and not `region_label`.
- The label is the value the LLM should *display* to the user; the
  raw ID is internal.

If the LLM knew the first fact, it wouldn't waste a token asking
for the label dimension (when there is one). If it knew the second,
it would render the label, not the ID, in the Presenter.

The fix is a one-line prompt addition: *"Dimensions with a
registered `Lookup` have their label attached to the result row as
`<dim>__label` automatically. Prefer the label when rendering to
the user."*

The current prompt's `Lookup` rendering (`resolve_lookup` tool
docs, vocabulary lists) tells the LLM the catalog has the values.
It does not tell the LLM the *user* sees the labels. The defensive
guarantee is invisible to the thing it's defending against.

### 4. The compiler-vs-runtime boundary is principled and load-bearing

In the first critique I called this split "leaky." On rereading, it
isn't — the split is exactly the *point*.

The LLM writes a `SemanticQuery` with `dimensions=["orders.region"]`.
The catalog author intends the user to see the region label. The
*enrichment* can't happen in the compiler because that would change
the SQL the LLM wrote. The whole design is "let the LLM write what
it writes, then ensure the user gets the right data anyway." The
runtime step is the mechanism by which the catalog author's intent
is preserved against the LLM's forgetfulness.

This is the right model. The Python API exposes
`enrich_result(rows, dim, lookup, ctx)` as a free function for the
caller to invoke; the MCP server should do the same invocation
automatically (see §2). The first critique's framing of "free
function the caller has to remember" was wrong because it
misidentified the caller as the Python user. The actual caller, in
the text-to-SQL story, is the *MCP server*, and the server is
*us*. We control the server. We should call it.

### 5. `Lookup` and `enrich_result` describe two lives of the same vocabulary — and that is right

`Lookup.values` is rendered into the planner prompt (prompt time).
`enrich_result` walks the result rows and adds `<dim>__label` (result
time). Both touch the same vocabulary, both serve the same
defensive goal, but they happen at different layers for *necessary*
reasons: the planner prompt can't change the result, the result
can't influence the planner.

The split is correct. The implementation, however, has a gap: the
planner-prompt rendering of `Lookup.values` (in `semql_prompt`)
should explicitly mark the values as "labels that will appear in
the result" so the LLM knows the data is already there. (See §3.)

### 6. The `Join` model critique was overblown

The first critique called `Join.on: str` "too thin" and suggested a
discriminated union. On rereading, the critique applies to the SQL
contract generally (`Measure.sql`, `Dimension.sql`, `Segment.sql`
all use the same `{alias}` SQL-fragment convention) and is not
specific to `Lookup`/enrichment. It should be revisited as a
catalog-wide SQL-fragment question, not a `Join` question.

What the `Join` model *does* serve for the text-to-SQL story: it's
the cross-cube SQL join that an LLM *can* ask for. The LLM emits
`SemanticQuery(dimensions=["orders.region", "products.category"])`
and the compiler walks the join graph to find the path. The fan-out
detector (`_check_fan_out`) is the safety net that prevents the LLM
from accidentally inflating a `SUM`. Both are good and load-bearing.

### 7. `InlineDerived`, `CompareWindow`, `drill_paths`, `Presentation` are *expressive*, not *defensive*

These four mechanisms are about what the LLM can ask for. They are
not about guaranteeing the user sees something the LLM forgot. They
are *admitted* in their docstrings to be exploratory
("`InlineDerived`", `spec.py:194`: "the exploratory shape an LLM or
human reaches for in chat"). The Phase A restriction on
`InlineDerived` (same-cube operands) is a hint that the design
expects these to migrate to the catalog over time.

For the text-to-SQL story, the question is: are these mechanisms
earning their keep? If a chatbot is shipping a 10-cube catalog and
the LLM is emitting `derived_measures=[InlineDerived("conv_rate",
"ratio", ["orders.count", "users.count"])]` on every query, the
catalog author is going to declare `ConvRate` as a stable measure
within a week. The exploratory surface pays for itself in the first
day and then is a tax.

This is a *separate* critique from the defensive one. The defensive
story (`Lookup` / `enrich_result`) is the load-bearing one for
text-to-SQL. The expressive story is a useful LLM affordance that
should probably be cut down to "the LLM can ask for what's in the
catalog; everything else is the catalog author's job" — but that's
a different document.

---

## Detailed feedback

### `max_inline=50` is the wrong cap for the defensive story

`packages/semql/src/semql/model.py:1208`: `max_inline: int = 50`. The
docstring frames this as a prompt-rendering cap. For the defensive
story, the cap is irrelevant: the LLM never needs to see the
vocabulary *in the prompt* if the label is attached at result time.

The right cap is the *result time* cap: how many distinct IDs can
appear in a single result set? A regional breakdown might have 10;
a per-customer list might have 10 000. The cap should be on result
size, not prompt size — and the runtime should fall back to a
batch-enricher rather than truncating the vocabulary.

**Suggestion:** Rename `max_inline` to `max_prompt_inline` (vocabulary
size cap) and add a separate `max_result_batch` (batch id→label call
size). The defensive layer doesn't truncate; the expressive layer
does.

### The dynamic-vs-static split conflates two axes

This is the same critique as in the first pass; it survives the
revision because the dynamic-vs-static split matters *more* for the
defensive story.

A static `values=("EMEA","APAC","NA")` is a list the LLM never
needs to see. The runtime always attaches the label; the LLM
doesn't need to know the values exist. So a static `Lookup` is
*purely defensive* — it has no prompt-time role.

A dynamic `loader=lambda ctx: db.fetch_regions(...)` is a runtime
dependency the user has to budget for. The LLM *might* need to see
the values to plan a `Filter(op="in", values=[...])` — but the
defensive layer still attaches the label regardless.

The model conflates "where the values come from" with "when they're
needed." A clean split: `Vocabulary(values, labels)` (prompt-time
hint, optional) and `Enricher(loader)` (runtime guarantee, always
present). A static lookup has both; a dynamic lookup has only the
`Enricher`.

### `enrich_result` mutates in place

Same as the first pass: `packages/semql/src/semql/lookups.py:154-159`
mutates each row in place. The mutation is the *right* behaviour for
the defensive story (we're attaching a guaranteed column, not
returning an alternative view) but the in-place mutation should be
documented. Add a `# mutates rows in place; the label column is
guaranteed attached, not optional` comment.

### `resolve` uses `difflib.SequenceMatcher` with a fixed 0.5 threshold

Same as the first pass; survives the revision. The threshold is a
prompt-time concern (LLM is asking "what values can I use?") and
doesn't matter for the defensive layer. But `resolve` is the function
the LLM uses via `resolve_lookup` in the MCP server, so a bad
threshold means the LLM emits `Filter(values=["EMAE"])` and the
filter returns nothing. The user's question "show me sales in
EMEA" returns an empty result and the LLM has no signal that the
typo was the cause.

**Suggestion:** On a fuzzy match below a higher confidence threshold
(0.7?), have the MCP tool return both the resolved value *and* a
`confidence: "exact" | "substring" | "fuzzy"` flag. The LLM can use
the flag to render "Did you mean EMEA?" in the Presenter.

### `Lookup.loader` has no contract for return types

Same as the first pass; survives the revision. The defensive story
makes this *more* important: the runtime always calls `enrich`, so a
malformed loader return value means *every* result row from a query
that touches that dimension is broken. The validation cost is
worthwhile.

### `drill_paths` is the right idea but the wrong shape

`packages/semql/src/semql/model.py:711-716`: `drill_paths:
list[list[str]]` is "pure metadata" consumed by the Drilldown
prompt stage. For the text-to-SQL story, the Drilldown suggestion
*is* a defensive feature: the LLM emits a result, the user might
ask "drill to state," the catalog declares the drill paths so the
LLM doesn't have to invent them.

But the drill paths are *invisible to the runtime*. The
`DrilldownSuggestion` (`plan.py:138`) carries a `query:
SemanticQuery` that the LLM emits — the catalog's declared paths
are *suggestions* to the LLM, not guarantees to the user. So if the
LLM doesn't see the drill paths (e.g. the prompt is too long and
the drill-paths block is truncated), the user doesn't get drills.

The fix is the same shape as the `Lookup` fix: emit the drill paths
into the planner prompt *unconditionally for the cube the user is
looking at*, and into the Drilldown prompt's `focused_row` block
*unconditionally for the focused cube*. A defensive layer that
disappears under prompt pressure is not a defensive layer.

### `Presentation` and `DrilldownSuggestion` are LLM outputs, not guarantees

`Presentation` (`plan.py:114`) and `DrilldownSuggestion` (`plan.py:138`)
are the LLM's outputs. They are not defensive — the LLM is the one
emitting them, and the LLM can emit whatever it wants. They're
expressive affordances for the LLM, not guarantees for the user.

The user-facing guarantee is in `enrich_result` (label attached to
row) and *could* be in a `default_presentation_template` (catalog
declares "if the LLM emits a 1-row result with one measure, render
as: `<label>: <measure>`") — but that's a different mechanism.

The first critique over-weighted `Presentation` and `Drilldown` as
"enrichment." On rereading, they're expressive: they let the LLM
shape the user-facing answer. They're not load-bearing for the
defensive story.

### `InlineDerived` is exploratory, not defensive

`InlineDerived` is the LLM asking for a derived measure the catalog
doesn't have. The user story is "show me `revenue / count`." The
defensive layer doesn't help here — there's no catalog author
intent being preserved. The LLM asked for something; the catalog
served it.

This is a useful affordance but it's *not* part of the
`Lookup`/enrichment story. Mixing the two critiques in the first
pass was a mistake. The right framing: `InlineDerived` is a
catalog-evolution affordance, and the question of whether to keep
it is independent of the question of whether `enrich_result` is
working.

---

## Questions for the author

1. **The MCP server doesn't call `enrich_result`. Is that intentional
   or a gap?** The `Lookup`/`enrich_result` machinery exists to
   serve the LLM-driven text-to-SQL story. The LLM is the consumer
   of the MCP server. The server returns raw rows. This looks like
   a gap, not a design choice. The MCP package is the recipe-shaped
   surface that owns the call site; the fix lives there.

2. **The Python API exposes `enrich_result` as a free function but
   provides no helper that uses it. Is that intentional?** The
   Pydantic-AI / Anthropic / OpenAI Agents / custom-harness chatbot
   author has to write the compile → execute → enrich loop
   themselves. Most will forget the enrich step. Is the helper not
   provided because it's out of scope, or because the design
   intends for the *recipe* to ship the helper, not core?

3. **The planner prompt doesn't tell the LLM the guarantee exists.
   Is that intentional?** The LLM doesn't know that
   `Lookup`-registered dimensions have their label attached for
   free. The LLM either wastes a turn asking for the label, or
   doesn't ask and the user sees the ID. This looks like a gap. The
   planner prompt is a recipe (`semql-prompt`); the fix lives
   there.

4. **Is the `Lookup` model too coupled to prompt-time concerns?**
   `max_inline` is a prompt-rendering cap. The defensive layer
   doesn't need a prompt-rendering cap. The values can be huge; the
   runtime batches the id→label call. The `values=` field has
   dual-use (prompt-rendering *and* runtime enrichment) and the two
   use cases have different constraints.

5. **What is the user story for `Lookup.values` in the prompt-time
   context, given that the runtime attaches the label?** If the
   LLM never needs to know the values (the defensive layer always
   attaches the label), then `values` in the prompt is redundant.
   The LLM can use `Filter(values=["EMEA"])` based on a Glossary
   entry ("EMEA" is a business term) and the runtime attaches the
   label. The `Lookup.values` is then purely the runtime's
   vocabulary.

6. **Is the `enrich_result` guarantee strong enough?** Currently it
   is: "the label is attached if a `Lookup` is registered and the
   `loader` is a `LookupEnricher`." But the catalog author has to
   *remember* to register a `Lookup` on every dimension that has a
   label. Is there a way to derive the `Lookup` from a `Join` (e.g.
   if the dimension is a foreign key to a `regions` cube, derive a
   `Lookup` from the join target)?

7. **The library-not-framework principle has a corollary: there
   must be a named tier in PHILOSOPHY.md.** "Core ships primitives;
   recipes wire them up" is implied by the existing
   `graphql-borrowed.md` / PHILOSOPHY.md language and the
   `bj3fkv8a1.txt` roadmap, but it is not yet a named principle.
   Should it be? (See Suggestions at the end of this critique.)

---

## Alternatives (revised)

The first critique proposed ten alternatives, several of which
(`A`, `C`, `H`, `I`) suggested moving the join to compile time. **All
of those are wrong** for the text-to-SQL story, because they would
require the LLM to write the join — which is the failure mode the
design is trying to defend against.

The alternatives that survive the revision are the ones that
*strengthen* the runtime guarantee or *make the guarantee visible*
to the LLM.

### J. Make `enrich_result` the default in MCP `query_execute`

**Idea:** Keep the `Lookup` model and the runtime split, but make
`enrich_result` the default behaviour of `query_execute` when a
Lookup is registered for a returned dimension.

- **User story:** "I'm an LLM-driven chatbot. I call `query_execute`
  over MCP. The result rows have the label column automatically. I
  render the label to the user."
- **Cost:** `query_execute`'s return type grows an
  `enrichment: dict[str, str]` map (which dimension was enriched,
  how) so the consumer can opt out per call (`enrich=False`).
- **Trade-off:** The compiler-vs-runtime split stays. The "enrich by
  default" behaviour is opt-out per call. The 90% case just works.
- **Tier of fix:** recipe (MCP). The MCP package is *already*
  framework-shaped (it owns the JSON-RPC boundary, the tool
  registration, the executor). Adding `enrich_result` to its
  `query_execute` is the right place to make the opinionated
  choice. It does not push core toward framework; it pushes the
  recipe toward correct behaviour.
- **Realistic outcome:** The MCP server ships labels in 90% of
  cases. The 10% who want raw IDs (a debugging tool, a low-level
  data export) opt out.

**This is the load-bearing change for the MCP surface.** Without it,
the `Lookup`/`enrich_result` machinery is a Python-only feature that
doesn't reach the LLM consumer it's designed for.

### J2. Ship `compile_and_run` in a `semql-pydantic-ai` recipe (not in core)

**Idea:** *Reverse of the first pass.* Add a
`compile_and_run(query, catalog, *, viewer=..., context=...,
executor, enrich=True) → list[dict]` helper — but in a *new recipe
package*, not in `semql/__init__.py`. The recipe is the planned
`semql-pydantic-ai` (S1 in the roadmap scratchpad) or a sibling
`semql-chatbot` recipe.

- **User story:** "I'm building a Pydantic-AI / Anthropic / OpenAI
  Agents chatbot. My tool function is three lines: call
  `compile_and_run`, return the rows. The defensive layer is the
  default."
- **Cost:** One new package (already on the roadmap as S1). The
  helper walks `compiled.column_meta` to find the dimensions to
  enrich, builds the `ResolutionContext` from the same `viewer` /
  `context` that went into the compile, and calls `enrich_result`
  for each.
- **Trade-off:** The helper duplicates a small amount of MCP server
  logic. Both recipes emit the same `Result[rows, enrichment_meta]`
  envelope. The cost is duplication; the benefit is that core stays
  a library and PHILOSOPHY.md's *"The compiler has no I/O"* is
  preserved.
- **Tier of fix:** recipe. The fix is exactly the kind of
  opinionated integration the recipe tier exists for. The package
  is allowed to know about executors, the result-enrichment
  contract, and the caller's intent to "just give me labelled rows"
  — that is its job.
- **Realistic outcome:** The Pydantic-AI chatbot author writes
  `@tool def run_query(q): return make_query_tool(catalog, ...).run(q)`.
  The result rows have the labels. The author has read the
  *recipe's* docstring, not the three core docstrings of
  `compile_query` + `enrich_result` + `ResolutionContext`. Core
  surface is unchanged.

**This is the load-bearing change for the Python API surface.**
Symmetric to J — closes the same gap, on the other chatbot
surface. Doing J in the MCP package and J2 in the recipe package
keeps core as a library and the recipes as the opinionated
integration layer.

### K. Make the defensive guarantee visible in the planner prompt

**Idea:** The planner prompt currently renders `Lookup.values` as a
vocabulary list. Add a one-line note: *"Dimensions with a registered
`Lookup` have their label attached to the result row as
`<dim>__label` automatically. Prefer the label when rendering to the
user."*

- **User story:** "I'm the planner LLM. I know that asking for
  `region` returns a result where I can render `<region__label>`
  without asking for it as a separate dimension."
- **Cost:** A few lines in `semql_prompt`'s `Lookup` rendering.
- **Trade-off:** None. The LLM uses the vocabulary list to plan
  filters (same as before) and now also knows the label is
  attached. The Presenter uses the label by default.
- **Realistic outcome:** The LLM stops asking for the label
  dimension when it's not needed. The Presenter renders labels
  instead of IDs. The user sees "EMEA" instead of "12".

**This is the second load-bearing change.** Together with J, it
closes the text-to-SQL story end-to-end.

### L. `Vocabulary` vs `Enricher` split

> **Status: IMPLEMENTED (2026-06-14)**, with a lighter shape than the
> sub-model split proposed below. Rather than splitting `Lookup` into a
> discriminated union, the two jobs became two *orthogonal fields* on the
> one `Lookup`:
>
> - **Vocabulary** (prompt-time): `values=` (static) or `loader=`
>   (dynamic), mutually exclusive — unchanged.
> - **Enricher** (post-query): a new `enricher=` slot
>   (`LookupEnricher | MultiFieldEnricher`), **never** materialised into
>   the prompt. `enrich_result` reads `enricher`, not `loader`.
>
> A `Lookup` may now be vocabulary-only, enricher-only (no `values`/`loader`
> — the LLM never sees the keyspace), or both. `sql_enricher` lost its
> plan-time `__call__`: it is a pure post-query enricher and is assigned to
> `enricher=`. This is the load-bearing fix for "don't leak reference-table
> ids (UUIDs, customer keys) into the planner prompt; label silently after
> the rows return." The single-model-with-two-fields shape was chosen over
> the union because it keeps the common "static enum + label" case a
> one-liner and avoids a four-flavour discriminated union. The remaining
> items below describe the originally-proposed union for the record.

**Idea:** Split `Lookup` into two sub-models. `Vocabulary(values,
labels)` is the prompt-time hint (what the planner sees).
`Enricher(loader)` is the runtime guarantee (what the user gets).
A static lookup has both. A dynamic lookup has only `Enricher`.

- **User story:** "I want to register a `Lookup` purely for the
  runtime guarantee, without forcing the LLM to see the vocabulary."
  / "I want to register a vocabulary hint for the LLM without
  paying the cost of a runtime enrichment."
- **Cost:** The `Lookup` model becomes a discriminated union. The
  existing model is the `StaticAndEnricher` flavour. Two new
  flavours: `VocabularyOnly` and `EnricherOnly`.
- **Trade-off:** Three or four `Lookup` flavours instead of one. The
  `LookupEnricher` protocol becomes the `Enricher.loader` contract.
- **Realistic outcome:** A `Lookup(dimension, values=..., labels=...)`
  with no `loader` is a vocabulary hint. A `Lookup(dimension,
  loader=batch_api_lookup_enricher)` with no values is a defensive
  guarantee that the LLM doesn't see. The two use cases don't
  conflate.

### M. Derive `Lookup` from `Join` + `primary_key`

**Idea:** If a dimension is a foreign key (`dim.foreign_key`) to
another cube, and the target cube has a `name` or `label` field,
auto-register a `Lookup(dimension, values=target.<label>)` on the
catalog. The catalog author writes a `Join`; the system writes the
`Lookup`.

- **User story:** "I have `orders.region_id` joining to
  `regions.id` with `regions.name`. The label is `regions.name` for
  free; I don't have to register a `Lookup` separately."
- **Cost:** The auto-derivation in `catalog.py:102` grows to also
  emit `Lookup`s. A new `Cube.many_to_one_label_field: str | None`
  field names the label column on the target.
- **Trade-off:** The `Lookup` becomes a derived model, not a primary
  one. Catalog authors can still register explicit `Lookup`s for
  non-join sources.
- **Realistic outcome:** 80% of `Lookup`s in a real catalog are
  derived from `Join`s. The catalog author writes less; the
  defensive guarantee is the default.

### N. Batch the runtime enrichment

**Idea:** `enrich_result` currently calls `enrich(ids, ctx)` once
per result set, with all distinct IDs. For very large result sets
(10 000 rows × 10 dimensions), this is fine. For very large
vocabularies (1 000 000 regions), the batch call is the bottleneck.

Add a `Lookup.enricher_batch_size: int = 1000` field. The runtime
chunks the ID list and calls the enricher in batches, with a
configurable per-batch concurrency.

- **User story:** "I have a million-customer lookup. The result set
  has 10 000 distinct customer IDs. The enricher hits a remote API
  and the call takes 30 seconds. I want to batch."
- **Cost:** The `enrich_result` function grows a loop. The
  `LookupEnricher` protocol grows a "batches are OK" expectation.
- **Trade-off:** The `LookupEnricher` is the bottleneck either way;
  batching just changes the per-call latency vs. total-latency
  trade-off.
- **Realistic outcome:** This is a `Lookup` model field, not a
  core change. The defensive story doesn't depend on it.

### O. Static lookup values rendered as a *default filter* vocabulary, not a *result* vocabulary

**Idea:** The planner prompt currently renders `Lookup.values` as
"the user might filter by these." Reframe: "the user *must* filter
by these, because the LLM can't ask for an unknown value without
the `resolve_lookup` tool." The vocabulary list is a constraint on
the LLM's filter choices, not a hint.

- **User story:** "I want the LLM to know that `region` only takes
  `EMEA`, `APAC`, `NA` — and that asking for `region=Mars` is a
  refuse-at-compile, not a runtime empty result."
- **Cost:** The compiler grows a check: "if the dimension has a
  registered `Lookup`, refuse a `Filter(values=[...])` whose values
  are not in the vocabulary." The LLM either asks the user to
  clarify, or calls `resolve_lookup` first.
- **Trade-off:** Tightens the LLM's contract. A `Lookup` is now a
  *constraint*, not just a *vocabulary*. The LLM either works
  within the constraint or escalates.
- **Realistic outcome:** The defensive layer becomes a *compile-time
  guarantee* for filter values. The runtime guarantee (label
  attached) is unchanged. The catalog author has a single
  mechanism: declare a `Lookup` and the dimension is fully
  controlled (valid filter values + attached label).

---

## What the alternatives collectively suggest

Reading the seven surviving alternatives together, three patterns
emerge:

1. **The defensive guarantee needs to reach the LLM consumer on
   *both* chatbot surfaces — via the recipes that own each
   surface, not via core.** J (MCP `query_execute` calls
   `enrich_result` by default), J2 (recipe package ships a
   `compile_and_run` helper for Pydantic-AI / Anthropic /
   custom-harness chatbot authors), and K (planner-prompt mentions
   the guarantee) are the load-bearing changes. **All three are
   recipe changes; core is unchanged.** Without them, the design
   intent is not implemented for either surface. The cost of each
   is small (closure call in MCP, helper function in the new
   recipe package, one prompt line in `semql-prompt`).

2. **`Lookup` is conflating two jobs.** L (Vocabulary vs Enricher
   split) and M (derive from Join) say the same thing: a `Lookup`
   is either a *prompt-time hint* (vocabulary the LLM sees) or a
   *runtime guarantee* (label attached to the row) or both. The
   current model treats them as one and the defensive story suffers
   (a static lookup pays for prompt rendering it doesn't need).
   These are core-model changes; recipes are irrelevant here.

3. **The `Lookup` vocabulary can be a *compile-time constraint*.** O
   (refuse filters with values not in the vocabulary) says the
   `Lookup` can do more than attach a label — it can prevent the
   LLM from emitting a query the catalog author knows is wrong.
   This is a tighter integration between the prompt-time and the
   compile-time layers, and it makes the catalog author a real
   participant in the LLM's planning. Also a core-model change.

If I had to pick three of the seven, the load-bearing ones are
**J + J2 + K** — all in recipes, none in core:

- **J** closes the gap on the MCP surface: `query_execute` calls
  `enrich_result` by default. Tier: recipe (MCP package).
- **J2** closes the gap on the Python API surface:
  `compile_and_run` is the one-liner for Pydantic-AI / Anthropic /
  custom-harness chatbot authors. Tier: recipe
  (`semql-pydantic-ai` / `semql-chatbot`).
- **K** closes the gap on the planner surface: the LLM knows the
  guarantee exists, so it stops wasting turns asking for the label
  dimension and the Presenter renders labels instead of IDs.
  Tier: recipe (`semql-prompt`).

Together, the three implement the text-to-SQL story as the design
intends, on both chatbot surfaces, with the LLM aware of the
guarantee — and core stays a library.

The deepest critique, though, is structural: **the
`Lookup`/`enrich_result` design is sound for the text-to-SQL
story, but the design is not connected to the LLM consumers that
story exists to serve, because the recipes that would connect
them are not yet built.** The Python API has the helper; the MCP
server doesn't call it; the planner prompt doesn't mention the
guarantee; the planned `semql-pydantic-ai` recipe doesn't exist
yet. The model is doing the right thing in isolation; the system
is not doing the right thing in integration — and the chatbot
surfaces are three of them (MCP, Pydantic-AI recipe, and the
Python API), each owned by a different recipe.

## Suggestions

Three concrete suggestions, each at the right tier.

1. **Add a "core vs. recipe" tier to `PHILOSOPHY.md`.** The
   principle "core ships primitives; recipes wire them up" is
   implied by the existing language (`PHILOSOPHY.md:96`, the
   `graphql-borrowed.md` discussion) and the `bj3fkv8a1.txt`
   roadmap, but it is not yet a named principle. Adding it would
   make the tier explicit and pre-empt the J2-in-core mistake. A
   one-paragraph addition to the "What SemQL is not" section.

2. **File three tickets, one per recipe change.** J (MCP
   `query_execute` calls `enrich_result` by default), J2 (recipe
   package ships `compile_and_run`), K (`semql-prompt` planner
   mentions the guarantee). Each is a small, independent change.
   The MCP and prompt changes can land before the recipe package
   exists. The recipe change is sequenced behind the S1 package
   landing.

3. **Add a property test that asserts the load-bearing invariant
   of the runtime defensive layer: any `CompiledQuery` returned
   for a query that touches a `Lookup`-registered dimension
   should be such that a downstream caller following the recipe
   gets the label column.** This is testable in the MCP package
   and in the recipe package independently. The core stays
   unopinionated; the recipes are testable against the contract.
