"""Score query complexity (1-10) and turn it into a price."""

from __future__ import annotations

import re

from backend.ledger import usd_to_micros

_COMPARISON_RE = re.compile(r"\b(vs|versus|compare|comparison|difference|better)\b", re.I)
_MULTI_SOURCE_RE = re.compile(r"\b(latest|recent|news|current)\b", re.I)

PRICE_PER_POINT_USD = 0.001


def score_query(query: str) -> int:
    """Return a complexity score from 1 (trivial) to 10 (very complex)."""
    score = 1
    words = query.split()
    score += len(words) // 50

    if _COMPARISON_RE.search(query):
        score += 2
    if _MULTI_SOURCE_RE.search(query):
        score += 2
    if _has_named_entity(query):
        score += 1
    if query.rstrip().endswith("?") and len(words) > 10:
        score += 2

    return min(score, 10)


def _has_named_entity(query: str) -> bool:
    """True if any sentence has a capitalized word after the first token."""
    for sentence in re.split(r"[.!?]+", query):
        tokens = sentence.strip().split()
        for token in tokens[1:]:
            if token and token[0].isupper():
                return True
    return False


def score_to_price(score: int) -> float:
    """Complexity score -> USD (score 5 -> 0.005)."""
    return round(score * PRICE_PER_POINT_USD, 4)


def price_micros(query: str) -> tuple[int, int]:
    """Return ``(score, price_in_micros)`` for a query."""
    score = score_query(query)
    return score, usd_to_micros(score_to_price(score))
