from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from backend.main import build_services, create_app
from backend.search import SearchError, claude_search, make_searcher
from tests.conftest import FakeAnthropic, make_settings, response, text_block, tool_use_block


def _web_result(url, title):
    return NS(type="web_search_result", url=url, title=title, page_age="2 days ago")


def test_claude_search_parses_results_and_citation_snippets():
    fake = FakeAnthropic([
        response(
            "end_turn",
            NS(type="server_tool_use", id="srv_1", name="web_search", input={"query": "q"}),
            NS(type="web_search_tool_result", tool_use_id="srv_1",
               content=[_web_result("https://a.com", "A"), _web_result("https://b.com", "B")]),
            NS(type="text", text="- Point one", citations=[NS(url="https://a.com", cited_text="quoted from A")]),
        )
    ])
    found = claude_search("q", fake, "claude-opus-5")
    assert found["provider"] == "claude"
    assert [r["url"] for r in found["results"]] == ["https://a.com", "https://b.com"]
    assert found["results"][0]["snippet"] == "quoted from A"
    assert found["summary"] == "- Point one"
    assert fake.messages.calls[0]["tools"][0]["type"] == "web_search_20260209"


def test_claude_search_resumes_after_pause_turn():
    fake = FakeAnthropic([
        response("pause_turn", NS(type="server_tool_use", id="srv_1", name="web_search", input={})),
        response("end_turn", NS(type="web_search_tool_result", tool_use_id="srv_1", content=[_web_result("https://a.com", "A")])),
    ])
    assert claude_search("q", fake, "m")["results"][0]["url"] == "https://a.com"
    assert fake.messages.calls[1]["messages"][-1]["role"] == "assistant"


def test_claude_search_error_block_raises():
    fake = FakeAnthropic([
        response("end_turn", NS(type="web_search_tool_result", tool_use_id="s", content=NS(error_code="max_uses_exceeded")))
    ])
    with pytest.raises(SearchError, match="max_uses_exceeded"):
        claude_search("q", fake, "m")


def test_auto_provider_prefers_brave_then_claude_then_mock():
    assert make_searcher(make_settings(search_provider="auto", brave_search_api_key="k"))[1] == "brave"
    assert make_searcher(make_settings(search_provider="auto"), anthropic_client=object())[1] == "claude"
    assert make_searcher(make_settings(search_provider="auto"))[1] == "mock"


def test_paid_but_search_failed_is_reported_honestly():
    fake = FakeAnthropic([
        response("tool_use", tool_use_block("search_web", {"query": "hi"})),
        response("end_turn", text_block("Search failed after payment.")),
    ])
    svc = build_services(make_settings(), anthropic_client=fake, setup_stripe=False)

    def broken(_q):
        raise SearchError("provider down")

    svc.agent._search = broken
    with TestClient(create_app(svc)) as client:
        data = client.post("/chat", json={"message": "x"}).json()
    call = data["tool_calls"][0]
    assert call["status"] == "paid_search_failed" and "provider down" in call["error"]
    assert call["reference"].startswith("0x")  # the payment did settle
    tool_result = fake.messages.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
