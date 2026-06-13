"""Pluggable retrieval over catalog cubes — grounding surface.

Two Protocols sit at the seam between catalog and search backend:

- :class:`EmbeddingProvider` — caller-supplied embedder. Both
  ``embed_sync`` and ``embed_async`` exist so a sync caller doesn't
  pay asyncio overhead and an async caller doesn't pay ``run_in_thread``
  overhead.

- :class:`Retriever` — opaque "return top-k cube names" surface.
  Callers can swap the in-memory defaults for pgvector / pinecone /
  chromadb / etc. without learning a SemQL-specific API.

Four default implementations ship in-tree:

- :class:`SQLiteBM25Retriever` — *zero-config default*. Stdlib
  ``sqlite3`` in-memory + FTS5 + the built-in ``bm25()`` ranking
  function. Wins on acronyms, exact identifier hits, and glossary
  aliases.
- :class:`NumpyCosineRetriever` — eager-embeds at construction, stores
  vectors in one ``(n_cubes, embed_dim)`` numpy array, computes cosine
  via a single matmul. Requires ``semql[retrieval]`` extras.
- :class:`HybridRetriever` — *recommended default when an
  ``EmbeddingProvider`` is configured*. Runs BM25 + cosine and fuses
  the ranked lists via Reciprocal Rank Fusion (RRF).
- :class:`MMRWrapper` — opt-in diversity reranker over any
  vector-bearing retriever; penalises near-duplicate cubes in the
  returned top-k.

Sized for catalogs up to ~10k cubes. Beyond that, plug a real vector
store in via the ``Retriever`` Protocol.
"""

from __future__ import annotations

import sqlite3
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from semql.model import Cube, GlossaryEntry


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Caller-supplied embedder. Implementations should be deterministic
    for cache-friendliness — same text + same ``model_id`` returns the
    same vector.

    ``model_id`` is a stable identifier (e.g. ``"text-embedding-3-small"``).
    SemQL caches embeddings by ``(text, model_id)``, so changing the
    underlying model — even with the same class — must change
    ``model_id`` so stale vectors don't leak.
    """

    model_id: str

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text. Vectors share a dimension."""
        ...

    async def embed_async(self, texts: list[str]) -> list[list[float]]:
        """Async variant — same contract as ``embed_sync``."""
        ...


@runtime_checkable
class Retriever(Protocol):
    """Returns ``(cube_name, score)`` tuples for a user query. Higher
    score = more relevant. ``k`` is a hard cap; implementations may
    return fewer when fewer cubes match."""

    def top_k(self, user_query: str, k: int) -> list[tuple[str, float]]: ...


# ---------------------------------------------------------------------------
# Document assembly — shared by all retrievers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Doc:
    """One indexed document. ``cube_name`` is what gets returned in
    top_k results; ``kind`` distinguishes cube docs from glossary
    aliases. Aliases all share their canonical entry's resolution
    surface — the retriever returns the alias's pointer, the caller
    follows it back to the term."""

    cube_name: str
    text: str
    kind: str = "cube"  # "cube" | "glossary"


def _cube_doc_text(cube: Cube) -> str:
    """Concatenated text: description + each question +
    space-joined keywords. One document per cube."""
    parts: list[str] = []
    if cube.description:
        parts.append(cube.description)
    parts.extend(cube.questions)
    if cube.keywords:
        parts.append(" ".join(cube.keywords))
    return "\n".join(parts).strip() or cube.name


def _build_docs(
    cubes: Sequence[Cube],
    glossary: Sequence[GlossaryEntry] | None = None,
) -> list[_Doc]:
    """Walk cubes and glossary aliases into a flat doc list. Glossary
    aliases are indexed separately so a misspelled / aliased term
    routes back to its canonical entry; the alias doc carries the
    *term* as its cube_name pointer (callers know to interpret a
    glossary hit accordingly via ``kind``)."""
    docs: list[_Doc] = []
    # Skip deprecated cubes — the compiler refuses them, so indexing
    # them just wastes retriever calls.
    for c in cubes:
        if c.stability == "deprecated":
            continue
        docs.append(_Doc(cube_name=c.name, text=_cube_doc_text(c), kind="cube"))
    if glossary:
        for g in glossary:
            # Each alias gets its own doc — separate FTS5 row / cosine
            # vector — and points back at the canonical term so a
            # match resolves consistently regardless of spelling.
            docs.append(_Doc(cube_name=g.term, text=g.term, kind="glossary"))
            for a in g.aliases:
                docs.append(_Doc(cube_name=g.term, text=a, kind="glossary"))
    return docs


# ---------------------------------------------------------------------------
# SQLiteBM25Retriever — stdlib sqlite3 + FTS5, no extras needed
# ---------------------------------------------------------------------------


class SQLiteBM25Retriever:
    """Lexical BM25 over an in-memory SQLite FTS5 virtual table.

    Why SQLite FTS5 over a hand-rolled BM25:
    - Stdlib (``sqlite3`` module ships with CPython).
    - Battle-tested Unicode tokenizer (``unicode61`` removes diacritics).
    - Production-grade ``bm25()`` ranking function.
    - Phrase queries, prefix queries, NEAR operator — all free.

    FTS5 returns lower-is-better ranks (negative numbers); this class
    negates so higher = more relevant, matching the ``Retriever``
    contract. Documents are stored verbatim — no pre-stemming on our
    side — and the FTS5 tokenizer handles case-folding plus
    Unicode normalisation. Acronym recall comes from FTS5's behaviour
    of indexing both the lowercased and original-cased forms; an
    ``AOV`` token survives a query for ``aov``.

    Constructed via :meth:`from_cubes` so the assembly + index build
    happen together — keeping a partial-build retriever around is a
    bug source we don't need.
    """

    def __init__(self, docs: list[_Doc]) -> None:
        # In-memory connection — never persisted. ``check_same_thread``
        # is fine because retrievers are read-only after construction.
        self._docs = docs
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            # FTS5 virtual table with two stored columns + the
            # tokenizer config. ``tokenize`` accepts a space-separated
            # arg string; we ask for the standard Unicode tokenizer
            # with diacritic-stripping (so "café" matches "cafe") and
            # the porter stemmer (so "orders" matches "ordering").
            "CREATE VIRTUAL TABLE t USING fts5("
            "doc_id UNINDEXED, cube_name UNINDEXED, text, "
            "tokenize='porter unicode61 remove_diacritics 2')"
        )
        self._conn.executemany(
            "INSERT INTO t(doc_id, cube_name, text) VALUES (?, ?, ?)",
            [(i, d.cube_name, d.text) for i, d in enumerate(docs)],
        )

    @classmethod
    def from_cubes(
        cls,
        cubes: Sequence[Cube],
        glossary: Sequence[GlossaryEntry] | None = None,
    ) -> SQLiteBM25Retriever:
        return cls(_build_docs(cubes, glossary))

    def _escape(self, q: str) -> str:
        """FTS5 MATCH treats some punctuation as operators. The safest
        approach for a free-text user query is to wrap each whitespace-
        split token in quotes — turns ``"can't ship"`` into ``"can"
        "ship"`` which FTS5 then ANDs implicitly."""
        tokens = [t for t in q.split() if t.strip()]
        if not tokens:
            return ""
        # Strip embedded double-quotes per FTS5 docs — escape by
        # doubling, but simpler to just drop them in a free-text path.
        safe = ['"' + t.replace('"', "") + '"' for t in tokens]
        return " OR ".join(safe)

    def top_k(self, user_query: str, k: int) -> list[tuple[str, float]]:
        match_expr = self._escape(user_query)
        if not match_expr:
            return []
        # FTS5's ``bm25()`` is an auxiliary function — it can't be
        # used inside aggregates / GROUP BY. So we pull a pool larger
        # than ``k`` (to allow multiple docs per cube to compete) and
        # collapse to per-cube best scores in Python. ``bm25()`` is
        # negative; the lowest = best match. We negate to satisfy
        # the higher-is-better contract.
        pool_size = max(k * 4, 32)
        rows = self._conn.execute(
            "SELECT cube_name, bm25(t) AS r FROM t WHERE t MATCH ? ORDER BY r LIMIT ?",
            (match_expr, pool_size),
        ).fetchall()
        best: dict[str, float] = {}
        for name, raw in rows:
            score = -float(raw)
            if name not in best or score > best[name]:
                best[name] = score
        return sorted(best.items(), key=lambda p: -p[1])[:k]


# ---------------------------------------------------------------------------
# Numpy-backed retrievers — require semql[retrieval] extras
# ---------------------------------------------------------------------------


def _require_numpy() -> Any:  # noqa: ANN401 — numpy module type isn't stable across versions
    """Lazy numpy import with a friendly error message. The vector
    retrievers all gate on this so the lexical path stays dep-free."""
    try:
        import numpy as np
    except ImportError as exc:
        raise ImportError(
            "semql.retrieve vector retrievers require numpy. "
            "Install with `pip install 'semql[retrieval]'` (or add "
            "numpy to your project)."
        ) from exc
    return np


class NumpyCosineRetriever:
    """Cosine-similarity retrieval over per-cube embeddings.

    Construction is eager: every cube's text is embedded up front via
    the provided :class:`EmbeddingProvider`, vectors are L2-normalised
    and stacked into a single ``(n_docs, embed_dim)`` numpy array.
    Query time is one matmul + an argsort — fast enough for 10k
    cubes on a laptop.

    Memory cost: ``n_docs * embed_dim * 4`` bytes (float32). At
    ``embed_dim=1536`` that's ~6 MB per 1000 docs.
    """

    def __init__(
        self,
        docs: list[_Doc],
        embedder: EmbeddingProvider,
        *,
        _matrix: Any | None = None,  # noqa: ANN401 — internal numpy ndarray
    ) -> None:
        self._docs = docs
        self._embedder = embedder
        np = _require_numpy()
        if _matrix is not None:
            self._matrix = _matrix
        elif not docs:
            # No docs → empty matrix. Calling embed_sync with [] is
            # safe but we'd still trip on the norm computation; just
            # short-circuit.
            self._matrix = np.zeros((0, 1), dtype=np.float32)
        else:
            vectors = embedder.embed_sync([d.text for d in docs])
            mat = np.asarray(vectors, dtype=np.float32)
            # Row-normalise so dot product == cosine. Guard against
            # zero vectors (would NaN on division).
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._matrix = mat / norms

    @classmethod
    def from_cubes(
        cls,
        cubes: Sequence[Cube],
        embedder: EmbeddingProvider,
        glossary: Sequence[GlossaryEntry] | None = None,
    ) -> NumpyCosineRetriever:
        return cls(_build_docs(cubes, glossary), embedder)

    @property
    def matrix(self) -> Any:  # noqa: ANN401
        """The stacked, row-normalised embedding matrix. Exposed for
        MMR (which needs the document vectors for pairwise sim
        computation)."""
        return self._matrix

    @property
    def docs(self) -> list[_Doc]:
        return self._docs

    def top_k(self, user_query: str, k: int) -> list[tuple[str, float]]:
        if not self._docs:
            return []
        np = _require_numpy()
        q_vec = np.asarray(self._embedder.embed_sync([user_query])[0], dtype=np.float32)
        norm = float(np.linalg.norm(q_vec))
        if norm == 0:
            return []
        q_vec = q_vec / norm
        sims = self._matrix @ q_vec  # (n_docs,)
        # Argsort descending. ``argpartition`` would be cheaper for
        # huge n; for ≤10k cubes the difference is noise.
        order = np.argsort(-sims)
        # Collapse multiple docs per cube to the best score.
        best: dict[str, float] = {}
        for idx in order:
            d = self._docs[int(idx)]
            s = float(sims[int(idx)])
            if d.cube_name not in best or s > best[d.cube_name]:
                best[d.cube_name] = s
            if len(best) >= k:
                break
        return sorted(best.items(), key=lambda p: -p[1])[:k]


# ---------------------------------------------------------------------------
# HybridRetriever — RRF over BM25 + cosine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HybridRetriever:
    """Reciprocal Rank Fusion over two sub-retrievers.

    Per the original RRF paper (Cormack et al. 2009), score per doc =
    ``Σ 1 / (k + rank_i)`` where ``rank_i`` is the doc's 1-indexed
    rank in retriever ``i``'s output. ``k=60`` is the paper's
    standard; higher ``k`` flattens differences between ranks.

    Pulls a *wider* candidate pool from each sub-retriever (``k *
    pool_multiplier`` each) so a doc that's rank 3 in retriever A and
    rank 50 in retriever B still contributes both ranks instead of
    only its A-side score.
    """

    bm25: Retriever
    cosine: Retriever
    rrf_k: int = 60
    pool_multiplier: int = 3

    def top_k(self, user_query: str, k: int) -> list[tuple[str, float]]:
        pool = max(k, k * self.pool_multiplier)
        a = self.bm25.top_k(user_query, pool)
        b = self.cosine.top_k(user_query, pool)
        scores: dict[str, float] = {}
        for rank, (name, _) in enumerate(a, start=1):
            scores[name] = scores.get(name, 0.0) + 1.0 / (self.rrf_k + rank)
        for rank, (name, _) in enumerate(b, start=1):
            scores[name] = scores.get(name, 0.0) + 1.0 / (self.rrf_k + rank)
        return sorted(scores.items(), key=lambda p: -p[1])[:k]


# ---------------------------------------------------------------------------
# MMRWrapper — diversity reranker
# ---------------------------------------------------------------------------


@dataclass
class MMRWrapper:
    """Maximal Marginal Relevance reranker.

    Wraps an inner retriever (typically cosine or hybrid). Pulls a
    larger candidate pool (``k * pool_multiplier``), then greedily
    selects the top-k trading off relevance vs novelty:

        score(d) = λ * relevance(d) - (1-λ) * max_{s in selected} sim(d, s)

    ``lambda_=1`` reduces to pure relevance (no MMR effect);
    ``lambda_=0`` is pure diversity. Default ``0.5`` is the standard
    balance.

    Requires the inner retriever to expose a per-doc vector matrix so
    pairwise similarity can be computed. ``NumpyCosineRetriever``
    works; a wrapped pure-BM25 retriever does not (raises at
    construction with a warning).
    """

    inner: Retriever
    matrix_source: NumpyCosineRetriever
    lambda_: float = 0.5
    pool_multiplier: int = 3

    def __post_init__(self) -> None:
        if not 0.0 <= self.lambda_ <= 1.0:
            raise ValueError(f"MMRWrapper.lambda_ must be in [0, 1], got {self.lambda_!r}.")
        if not self.matrix_source.docs:
            warnings.warn(
                "MMRWrapper: matrix_source has no docs; MMR will be a no-op.",
                stacklevel=2,
            )
        # Pre-index doc vectors by cube_name → row index for fast
        # lookup at query time. When a cube has multiple docs (glossary
        # alias hits), we use the first; MMR diversifies *cubes*, not
        # individual docs.
        self._row_by_cube: dict[str, int] = {}
        for i, d in enumerate(self.matrix_source.docs):
            self._row_by_cube.setdefault(d.cube_name, i)

    def top_k(self, user_query: str, k: int) -> list[tuple[str, float]]:
        np = _require_numpy()
        pool_n = max(k, k * self.pool_multiplier)
        candidates = self.inner.top_k(user_query, pool_n)
        if len(candidates) <= k:
            return candidates

        # Restrict to candidates that have vectors we can score sim
        # for; anything else falls through to pure-rank order at the
        # end.
        scored_idx: list[int] = []
        for name, _ in candidates:
            if name in self._row_by_cube:
                scored_idx.append(self._row_by_cube[name])
        if not scored_idx:
            return candidates[:k]

        matrix = self.matrix_source.matrix
        # Use the inner retriever's score as relevance; rescale to
        # [0, 1] so it composes with sim in the MMR formula. Min-max
        # over the candidate set keeps weights consistent.
        rel: dict[str, float] = {name: s for name, s in candidates}
        rel_vals = list(rel.values())
        lo, hi = min(rel_vals), max(rel_vals)
        span = hi - lo if hi > lo else 1.0
        rel_norm = {n: (s - lo) / span for n, s in rel.items()}

        selected_names: list[str] = []
        selected_rows: list[int] = []
        remaining = [(n, s) for n, s in candidates if n in self._row_by_cube]
        while remaining and len(selected_names) < k:
            best_name: str | None = None
            best_score = float("-inf")
            for n, _ in remaining:
                row = self._row_by_cube[n]
                if not selected_rows:
                    sim_max = 0.0
                else:
                    sims = matrix[selected_rows] @ matrix[row]
                    sim_max = float(np.max(sims))
                score = self.lambda_ * rel_norm[n] - (1.0 - self.lambda_) * sim_max
                if score > best_score:
                    best_score = score
                    best_name = n
            if best_name is None:
                break
            selected_names.append(best_name)
            selected_rows.append(self._row_by_cube[best_name])
            remaining = [(n, s) for n, s in remaining if n != best_name]

        # Return with the original inner-retriever score so callers
        # see comparable numbers across queries.
        out: list[tuple[str, float]] = [(n, rel[n]) for n in selected_names]
        if len(out) < k:
            # Pad with unscored candidates (those without vectors) at
            # the tail.
            picked = set(selected_names)
            for n, s in candidates:
                if n not in picked:
                    out.append((n, s))
                    if len(out) >= k:
                        break
        return out[:k]


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def build_default_retriever(
    cubes: Sequence[Cube],
    *,
    embedder: EmbeddingProvider | None = None,
    glossary: Sequence[GlossaryEntry] | None = None,
    mmr: bool = False,
    mmr_lambda: float = 0.5,
) -> Retriever:
    """Apply the selection policy:

    - No ``embedder`` → ``SQLiteBM25Retriever``.
    - With ``embedder`` → ``HybridRetriever(bm25, cosine)``.
    - ``mmr=True`` wraps the result in :class:`MMRWrapper`. Requires
      ``embedder`` (no vectors otherwise).
    """
    bm25 = SQLiteBM25Retriever.from_cubes(cubes, glossary=glossary)
    if embedder is None:
        if mmr:
            warnings.warn(
                "build_default_retriever: mmr=True requires an embedder "
                "(MMR needs vectors); falling back to BM25 only.",
                stacklevel=2,
            )
        return bm25
    cosine = NumpyCosineRetriever.from_cubes(cubes, embedder, glossary=glossary)
    hybrid = HybridRetriever(bm25=bm25, cosine=cosine)
    if mmr:
        return MMRWrapper(inner=hybrid, matrix_source=cosine, lambda_=mmr_lambda)
    return hybrid


__all__ = [
    "EmbeddingProvider",
    "HybridRetriever",
    "MMRWrapper",
    "NumpyCosineRetriever",
    "Retriever",
    "SQLiteBM25Retriever",
    "build_default_retriever",
]
