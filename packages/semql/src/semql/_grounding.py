"""Internal helpers for LLM-grounding metadata validation.

The Cube / SavedQuery / Catalog models all carry ``questions`` /
``keywords`` / ``relations`` fields with the same length caps and
normalisation rules. Centralising the helpers keeps validators on
three frozen-or-mutable models consistent without forcing one model
file to import another's internals.
"""

from __future__ import annotations

# Length caps. Soft limits that catch obvious mistakes (a 50KB
# paragraph pasted into ``relations``) without being so tight that
# genuine content gets clipped.
QUESTION_MAX_CHARS = 200
KEYWORD_MAX_CHARS = 50
RELATIONS_MAX_CHARS = 2000


def normalize_keyword(s: str) -> str:
    """Acronym-preserving normalisation: ALL-CAPS tokens stay verbatim,
    everything else lowercases. ``"AOV" → "AOV"``, ``"Aov" → "aov"``."""
    return s if s.isupper() else s.lower()


def dedupe_keywords(keywords: list[str]) -> list[str]:
    """Normalise then dedupe case-insensitively; first occurrence wins,
    insertion order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for kw in keywords:
        normalized = normalize_keyword(kw)
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out


def validate_questions(label: str, name: str, values: list[str]) -> list[str]:
    """Refuse empty entries, cap length, return as-is."""
    for q in values:
        if not q.strip():
            raise ValueError(f"{label} {name!r}: ``questions`` entries cannot be empty.")
        if len(q) > QUESTION_MAX_CHARS:
            raise ValueError(
                f"{label} {name!r}: question {q[:40]!r}… exceeds {QUESTION_MAX_CHARS} chars."
            )
    return values


def validate_keywords(label: str, name: str, values: list[str]) -> list[str]:
    """Refuse empty entries, cap length, normalise + dedupe."""
    for kw in values:
        if not kw.strip():
            raise ValueError(f"{label} {name!r}: ``keywords`` entries cannot be empty.")
        if len(kw) > KEYWORD_MAX_CHARS:
            raise ValueError(f"{label} {name!r}: keyword {kw!r} exceeds {KEYWORD_MAX_CHARS} chars.")
    return dedupe_keywords(values)


def validate_relations(label: str, name: str, value: str) -> str:
    if len(value) > RELATIONS_MAX_CHARS:
        raise ValueError(
            f"{label} {name!r}: ``relations`` exceeds "
            f"{RELATIONS_MAX_CHARS} chars ({len(value)} given)."
        )
    return value


__all__ = [
    "KEYWORD_MAX_CHARS",
    "QUESTION_MAX_CHARS",
    "RELATIONS_MAX_CHARS",
    "dedupe_keywords",
    "normalize_keyword",
    "validate_keywords",
    "validate_questions",
    "validate_relations",
]
