"""Web search providers.

SEARCH_PROVIDER picks one (default ``auto``):
  auto   - Brave if BRAVE_SEARCH_API_KEY is set, else Claude web search if an
           Anthropic key is set, else mock
  claude - Anthropic's server-side web search tool, using the existing API key
           (billed per search plus tokens on the Anthropic account)
  brave  - Brave Search API
  mock   - placeholder results (tests / offline)

Every provider returns ``{"query", "provider", "results": [{title, url, snippet}], "summary"?}``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from backend.config import Settings

logger = logging.getLogger("autowallet.search")

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
MAX_CONTINUATIONS = 3
MAX_RESULTS = 5

Searcher = Callable[[str], dict[str, Any]]
Reporter = Callable[[str], dict[str, Any]]


class SearchError(Exception):
    pass


def mock_search(query: str) -> dict[str, Any]:
    return {
        "query": query,
        "provider": "mock",
        "results": [
            {
                "title": f"Mock result {i + 1} for '{query}'",
                "url": f"https://example.com/search?q={query.replace(' ', '+')}&r={i + 1}",
                "snippet": "Placeholder result (SEARCH_PROVIDER=mock).",
            }
            for i in range(3)
        ],
    }


def brave_search(query: str, api_key: str) -> dict[str, Any]:
    try:
        resp = httpx.get(
            BRAVE_URL,
            params={"q": query, "count": MAX_RESULTS},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise SearchError(f"Brave search failed: {exc}") from exc
    results = [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
        for r in resp.json().get("web", {}).get("results", [])[:MAX_RESULTS]
    ]
    return {"query": query, "provider": "brave", "results": results}


def _claude_web(
    prompt: str, client: Any, model: str, *, max_uses: int, effort: str, max_tokens: int
) -> tuple[list[dict[str, str]], str]:
    """One Claude call with the web search tool. Returns (sources, text written after the last search)."""
    tool = {**WEB_SEARCH_TOOL, "max_uses": max_uses}
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    def create(msgs: list[dict[str, Any]]) -> Any:
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            tools=[tool],
            messages=msgs,
            extra_body={"output_config": {"effort": effort}},
        )

    try:
        response = create(messages)
        # A long server-side search loop can pause; resend to let the server resume.
        for _ in range(MAX_CONTINUATIONS):
            if response.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": response.content}]
            response = create(messages)
    except Exception as exc:  # anthropic.APIError and transport errors
        raise SearchError(f"Claude web search failed: {exc}") from exc

    results: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    text_parts: list[str] = []

    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "web_search_tool_result":
            text_parts = []  # keep only the text written after the (last) search
            content = block.content
            if isinstance(content, list):  # success: list of web_search_result
                for item in content:
                    url = getattr(item, "url", "")
                    if url and url not in results:
                        results[url] = {
                            "title": getattr(item, "title", "") or url,
                            "url": url,
                            "snippet": "",  # filled from citations when the API returns them
                            "age": getattr(item, "page_age", None) or "",
                        }
            else:  # error: a single object with error_code
                errors.append(str(getattr(content, "error_code", "unknown_error")))
        elif block_type == "text":
            text_parts.append(block.text)
            for citation in getattr(block, "citations", None) or []:
                url = getattr(citation, "url", "")
                cited = getattr(citation, "cited_text", "")
                if url in results and cited and not results[url]["snippet"]:
                    results[url]["snippet"] = cited

    if not results:
        raise SearchError(
            f"Claude web search returned no results ({', '.join(errors) or response.stop_reason})"
        )
    # Results Claude actually quoted come first; they carry snippets.
    ordered = sorted(results.values(), key=lambda r: not r["snippet"])
    return ordered, "".join(text_parts).strip()


def claude_search(query: str, client: Any, model: str) -> dict[str, Any]:
    """Run one query through Claude's server-side web search tool."""
    prompt = (
        f"Search the web for: {query}\n\n"
        "Then summarize what the most relevant, most recent results say in at most 5 short "
        "bullet points. Include dates where the sources give them. Only state what the "
        "sources support."
    )
    # Low effort: this call only runs a search and condenses it, so deep reasoning just adds latency.
    sources, summary = _claude_web(prompt, client, model, max_uses=2, effort="low", max_tokens=4000)
    return {"query": query, "provider": "claude", "results": sources[:MAX_RESULTS], "summary": summary}


def claude_report(topic: str, client: Any, model: str) -> dict[str, Any]:
    """Premium tool: several searches, then a structured, sourced research report."""
    prompt = (
        f"Write a research report on: {topic}\n\n"
        "Run several web searches from different angles (background, latest developments, numbers, "
        "criticisms or risks). Then write the report in Markdown with these sections: Overview, "
        "Key developments (with dates), By the numbers, Risks and criticisms, Outlook. Keep it under "
        "600 words. Only state what the sources support, and say when sources disagree."
    )
    sources, report = _claude_web(prompt, client, model, max_uses=5, effort="medium", max_tokens=8000)
    if not report:
        raise SearchError("report generation returned no text")
    return {"topic": topic, "provider": "claude", "report": report, "sources": sources[:8]}


def mock_report(topic: str) -> dict[str, Any]:
    return {
        "topic": topic,
        "provider": "mock",
        "report": f"## Overview\nPlaceholder report on {topic} (SEARCH_PROVIDER=mock).",
        "sources": mock_search(topic)["results"],
    }


def make_searcher(settings: Settings, anthropic_client: Any = None) -> tuple[Searcher, str]:
    """Return ``(search_fn, provider_name)`` for the configured provider."""
    provider = settings.search_provider
    if provider == "auto":
        if settings.brave_search_api_key:
            provider = "brave"
        elif anthropic_client is not None:
            provider = "claude"
        else:
            provider = "mock"

    if provider == "brave":
        if not settings.brave_search_api_key:
            raise ValueError("SEARCH_PROVIDER=brave needs BRAVE_SEARCH_API_KEY")
        return (lambda q: brave_search(q, settings.brave_search_api_key)), "brave"
    if provider == "claude":
        if anthropic_client is None:
            raise ValueError("SEARCH_PROVIDER=claude needs ANTHROPIC_API_KEY")
        model = settings.search_model or settings.claude_model
        return (lambda q: claude_search(q, anthropic_client, model)), "claude"
    return mock_search, "mock"


def make_reporter(search_provider: str, settings: Settings, anthropic_client: Any = None) -> Reporter:
    """Reports need Claude; use it unless search is mocked or no Anthropic client exists."""
    if search_provider == "mock" or anthropic_client is None:
        return mock_report
    model = settings.search_model or settings.claude_model
    return lambda topic: claude_report(topic, anthropic_client, model)
