"""Web search provider: Brave Search when BRAVE_SEARCH_API_KEY is set, otherwise mock results."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("autowallet.search")

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


def search(query: str, *, brave_api_key: str = "", count: int = 5) -> dict[str, Any]:
    if brave_api_key:
        try:
            resp = httpx.get(
                BRAVE_URL,
                params={"q": query, "count": count},
                headers={"X-Subscription-Token": brave_api_key, "Accept": "application/json"},
                timeout=15.0,
            )
            resp.raise_for_status()
            results = [
                {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
                for r in resp.json().get("web", {}).get("results", [])[:count]
            ]
            return {"query": query, "provider": "brave", "results": results}
        except httpx.HTTPError as exc:
            logger.warning("Brave search failed, falling back to mock: %s", exc)

    return {
        "query": query,
        "provider": "mock",
        "results": [
            {
                "title": f"Mock result {i + 1} for '{query}'",
                "url": f"https://example.com/search?q={query.replace(' ', '+')}&r={i + 1}",
                "snippet": "Placeholder result. Set BRAVE_SEARCH_API_KEY for real web results.",
            }
            for i in range(3)
        ],
    }
