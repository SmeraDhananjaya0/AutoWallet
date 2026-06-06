"""Score user query complexity on a 1–10 scale for payment routing."""

from __future__ import annotations

import re

_COMPARISON_WORDS = ("vs", "compare", "difference", "better")
_MULTI_SOURCE_WORDS = ("latest", "recent", "news", "current")


def score_query(query: str) -> int:
    """Return a complexity score from 1 (trivial) to 10 (very complex)."""
    score = 1
    words = query.split()
    score += len(words) // 50

    lower = query.lower()
    if any(word in lower for word in _COMPARISON_WORDS):
        score += 2
    if any(word in lower for word in _MULTI_SOURCE_WORDS):
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
    """Map a complexity score to a USD dollar amount.

    Returns dollars (e.g. score 5 → 0.005). Transactions store ``amount_usd``
    directly as a float for sub-cent micropayment display.
    """
    return round(score * 0.001, 4)


if __name__ == "__main__":
    _complex_query = (
        "How do recent advances in transformer architectures from OpenAI, Anthropic, "
        "and Google DeepMind compare in inference cost, SWE-bench coding scores, and "
        "safety alignment? What does the latest peer-reviewed research say about scaling "
        "laws for multimodal vision-language models combining text and images, and which "
        "open-source alternatives to GPT-4 are most viable for regulated enterprise "
        "deployment in healthcare and finance during 2025?"
    )

    _test_queries = [
        "hi",
        "what is python",
        "compare GPT-4 vs Claude for coding",
        "what are the latest developments in AI research this week?",
        _complex_query,
    ]

    for q in _test_queries:
        s = score_query(q)
        p = score_to_price(s)
        preview = q if len(q) <= 60 else q[:57] + "..."
        print(f"query: {preview!r}")
        print(f"  score: {s}  price: ${p:.4f}")
        print()
