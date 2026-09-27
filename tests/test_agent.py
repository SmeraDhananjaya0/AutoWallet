from __future__ import annotations

from fastapi.testclient import TestClient

from backend.main import build_services, create_app
from tests.conftest import FakeAnthropic, make_settings, response, text_block, tool_use_block


def _app_with(responses):
    fake = FakeAnthropic(responses)
    svc = build_services(make_settings(), anthropic_client=fake, setup_stripe=False)
    return svc, fake, TestClient(create_app(svc))


def test_search_is_charged_before_results_reach_claude():
    svc, fake, client = _app_with(
        [
            response("tool_use", tool_use_block("search_web", {"query": "latest AI funding"})),
            response("end_turn", text_block("Here is what I found.")),
        ]
    )
    with client:
        data = client.post("/chat", json={"message": "research AI funding"}).json()

    [call] = data["tool_calls"]
    assert call["status"] == "paid"
    assert call["rail"] == "robinhood_chain"
    assert call["reference"].startswith("0x")
    assert data["search_results"][0]["results"]
    assert data["balance_micros"] == 10_000_000 - int(call["amount_usd"] * 1_000_000)
    assert data["transactions"][0]["reference"] == call["reference"]

    # The tool result Claude saw carries the settled payment reference.
    tool_result = fake.messages.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is False
    assert call["reference"] in tool_result["content"]


def test_failed_payment_returns_error_and_no_results():
    svc, fake, client = _app_with(
        [
            response("tool_use", tool_use_block("search_web", {"query": "hi"})),
            response("end_turn", text_block("The payment failed.")),
        ]
    )
    svc.router.chain.available = lambda: (False, "down")
    svc.router.topup.enabled = False
    with client:
        data = client.post("/chat", json={"message": "search hi"}).json()

    assert data["tool_calls"][0]["status"] == "failed"
    assert data["search_results"] == []
    assert data["transactions"] == []
    tool_result = fake.messages.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert tool_result["content"].startswith("PAYMENT FAILED")


def test_paid_call_limit():
    calls = [
        response("tool_use", *[tool_use_block("search_web", {"query": f"q{i}"}, f"toolu_{i}") for i in range(4)]),
        response("end_turn", text_block("done")),
    ]
    svc, fake, client = _app_with(calls)
    with client:
        data = client.post("/chat", json={"message": "go"}).json()
    assert len(data["tool_calls"]) == 3
    results = fake.messages.calls[1]["messages"][-1]["content"]
    assert results[3]["is_error"] is True and "Limit" in results[3]["content"]


def test_auto_topup_surfaces_as_event_in_chat():
    svc, fake, client = _app_with(
        [
            response("tool_use", tool_use_block("search_web", {"query": "hi"})),
            response("end_turn", text_block("ok")),
        ]
    )
    with client:
        client.post("/demo/set-balance", json={"balance_usd": 0.0005})
        data = client.post("/chat", json={"message": "search"}).json()
    assert data["events"][0]["type"] == "auto_topup"
    assert data["events"][0]["status"] == "completed"
    assert data["tool_calls"][0]["status"] == "paid"
    # Claude is told about the refill and the post-charge balance.
    receipt = fake.messages.calls[1]["messages"][-1]["content"][0]["content"]
    assert '"auto_topups"' in receipt and '"balance_after_usd"' in receipt


def test_chat_without_api_key_is_503(client):
    assert client.post("/chat", json={"message": "hi"}).status_code == 503


def test_refusal_is_reported():
    svc, fake, client = _app_with([response("refusal")])
    with client:
        data = client.post("/chat", json={"message": "x"}).json()
    assert data["response"]
    assert data["tool_calls"] == []
