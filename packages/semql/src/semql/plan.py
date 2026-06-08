"""Typed output models for the four prompt roles.

Each role in the SemQL prompt pipeline emits a structured value:

- ``RouterDecision`` — semantic-vs-raw plus which cubes / views the
  question maps to. Consumed by the Query Generator.
- ``QueryPlan`` — one or more ``QueryStep``s, each tagged with an
  ``intent`` (headline / breakdown / compare / context) so the
  Presenter knows what role each result plays. CompiledQuery into SQL.
- ``Presentation`` — the user-facing narrative + highlights + caveats
  that goes back to the asker. Pairs with ``VizDecision`` (from
  ``semql.visualize``) which separately picks the chart shape.
- ``DrilldownSuggestion`` / ``DrilldownSuggestions`` — proposals for
  "what to ask next" given a row of interest. Each suggestion is a
  ``SemanticQuery`` ready to run, plus a user-facing label.

These types are pure Pydantic and have no LLM dependency. Pair them
with whatever model client you like (pydantic-ai, anthropic SDK, raw
HTTP) — the structured output is the contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from semql.spec import SemanticQuery

# Router output ----------------------------------------------------------------

RouteTo = Literal["semantic", "raw"]
"""Where the question gets answered. ``semantic`` routes through the
catalog + compiler; ``raw`` falls back to caller-emitted SQL for
shapes the catalog can't express (window functions, recursive CTEs,
pivots, columns not in the model)."""


class RouterDecision(BaseModel):
    """The first stage's output: which route, and on what surface.

    When ``route_to == "semantic"``, ``cubes`` and/or ``views`` name the
    catalog surface the Query Generator should scope to (a trimmed
    prompt fragment instead of the full catalog keeps subsequent
    stages crisp).

    When ``route_to == "raw"``, both lists are empty — the question doesn't
    fit the catalog and the caller is responsible for the SQL.

    ``reasoning`` is the LLM's own justification; carry it through
    rather than discarding so a downstream "why did you route this
    way?" debug surface has something to show. Pure prose, not part
    of the contract.
    """

    model_config = ConfigDict(frozen=True)
    route_to: RouteTo
    cubes: list[str] = Field(default_factory=list)
    views: list[str] = Field(default_factory=list)
    reasoning: str | None = None


# Query Generator output -------------------------------------------------------

QueryIntent = Literal["headline", "breakdown", "compare", "context"]
"""What role a given query plays in the answer:

- ``headline`` — the primary number the user asked for ("revenue last
  quarter").
- ``breakdown`` — a disaggregation alongside the headline ("revenue
  by region").
- ``compare`` — a sibling number for context ("revenue prior quarter").
- ``context`` — supporting data the answer references but doesn't
  feature ("row count for the period" to caveat sparsity).
"""


class QueryStep(BaseModel):
    """One compiled-but-not-yet-run query inside a ``QueryPlan``.

    ``intent`` lets the Presenter compose a coherent answer — the
    headline gets prose, breakdowns become tables / charts, compares
    surface as deltas. A bare ``list[SemanticQuery]`` would force the
    Presenter to re-infer intent from shape, which is fragile.

    ``label`` is an optional human-readable description (e.g. "Q4
    revenue vs Q3"); useful for prose generation and for the UI when
    showing the plan to the user.
    """

    model_config = ConfigDict(frozen=True)
    query: SemanticQuery
    intent: QueryIntent
    label: str | None = None


class QueryPlan(BaseModel):
    """The Query Generator's output. A list of typed ``QueryStep``s
    that compile and execute independently, plus optional reasoning.

    Empty ``steps`` is legal — it means the Generator could not
    formulate a query for the routed question. Treat it as "ask the
    user to rephrase," not as a successful answer.
    """

    model_config = ConfigDict(frozen=True)
    steps: list[QueryStep] = []
    reasoning: str | None = None


# Presenter output -------------------------------------------------------------


class Presentation(BaseModel):
    """The Presenter's output: the user-facing answer narrative.

    ``summary`` is the single-paragraph response — what an executive
    would read first. ``highlights`` and ``caveats`` are optional
    bullet lists for "what's interesting" and "what to be careful
    about" (small-sample warnings, missing data, etc.).

    Chart shape lives separately in ``VizDecision`` from
    ``semql.visualize`` — the Presenter narrates results; the
    visualiser picks the chart. Keep them decoupled so a text-only
    surface (chat, email) can render ``Presentation`` without
    fabricating a chart.
    """

    model_config = ConfigDict(frozen=True)
    summary: str
    highlights: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


# Drilldown output -------------------------------------------------------------


class DrilldownSuggestion(BaseModel):
    """One "what to ask next" proposal anchored to a row of interest.

    ``label`` is what a UI surfaces as a clickable option ("Break down
    Q4 revenue by region"). ``query`` is the ``SemanticQuery`` that
    runs when the user accepts the suggestion. ``rationale`` is
    optional prose explaining why this drill is interesting given the
    focused row — useful for tooltips or transcripts.
    """

    model_config = ConfigDict(frozen=True)
    label: str
    query: SemanticQuery
    rationale: str | None = None


class DrilldownSuggestions(BaseModel):
    """A small ordered set of ``DrilldownSuggestion``s.

    Cap the list size at the call site (3-5 is usually enough);
    this model doesn't enforce a cap to stay flexible. ``focus`` is
    optional prose describing the row the suggestions anchor to — a
    handy thing to round-trip through a UI for debugging.
    """

    model_config = ConfigDict(frozen=True)
    suggestions: list[DrilldownSuggestion] = []
    focus: str | None = None


__all__ = [
    "DrilldownSuggestion",
    "DrilldownSuggestions",
    "Presentation",
    "QueryIntent",
    "QueryPlan",
    "QueryStep",
    "RouterDecision",
    "RouteTo",
]
