from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from backend.main import build_services, create_app
from backend.rails import SETTLED, PaymentResult
from backend.search import SearchError
from tests.conftest import FakeAnthropic, make_settings, response, text_block, tool_use_block


def _setup(responses, reporter=None):
    fake = FakeAnthropic(responses)
    svc = build_services(make_settings(), anthropic_client=fake, setup_stripe=False)
    stripe = svc.router.stripe
    stripe.customer_id = "cus_test"  # mark the card rail ready without calling Stripe
    stripe.charge_card = MagicMock(return_value=PaymentResult("stripe", 750_000, SETTLED, reference="ch_test_1"))
    stripe.refund = MagicMock(return_value=PaymentResult("stripe", 750_000, SETTLED, reference="re_test_1"))
    if reporter:
        svc.agent._report = reporter
    return svc, fake


def _report_call(topic="Robinhood Chain"):
    return [
        response("tool_use", tool_use_block("deep_research_report", {"topic": topic})),
        response("end_turn", text_block("Here is your report.")),
    ]


def test_report_is_charged_on_stripe_before_it_is_written():
    order = []
    svc, fake = _setup(_report_call())
    svc.router.stripe.charge_card.side_effect = lambda *a, **k: (
        order.append("charge"),
        PaymentResult("stripe", 750_000, SETTLED, reference="ch_test_1"),
    )[1]

    def reporter(topic):
        order.append("report")
        return {"topic": topic, "provider": "mock", "report": "## Overview\nok", "sources": []}

    svc.agent._report = reporter
    with TestClient(create_app(svc)) as client:
        data = client.post("/chat", json={"message": "deep research report please"}).json()

    assert order == ["charge", "report"]
    [call] = data["tool_calls"]
    assert call["tool"] == "deep_research_report"
    assert call["rail"] == "stripe" and call["status"] == "paid"
    assert call["amount_usd"] == 0.75 and call["reference"] == "ch_test_1"
    assert data["reports"][0]["report"].startswith("## Overview")
    assert data["transactions"][0]["rail"] == "stripe"
    assert data["balance_micros"] == 10_000_000 - 750_000


def test_failed_report_refunds_the_card():
    def broken(_topic):
        raise SearchError("search backend down")

    svc, fake = _setup(_report_call(), reporter=broken)
    with TestClient(create_app(svc)) as client:
        data = client.post("/chat", json={"message": "report"}).json()

    call = data["tool_calls"][0]
    assert call["status"] == "refunded" and call["refund_reference"] == "re_test_1"
    svc.router.stripe.refund.assert_called_once()
    assert data["balance_micros"] == 10_000_000  # charge and refund cancel out
    assert [t["kind"] for t in data["transactions"]] == ["topup", "charge"]
    assert "refunded" in fake.messages.calls[1]["messages"][-1]["content"][0]["content"]


def test_only_one_report_per_request():
    calls = [
        response(
            "tool_use",
            tool_use_block("deep_research_report", {"topic": "a"}, "toolu_1"),
            tool_use_block("deep_research_report", {"topic": "b"}, "toolu_2"),
        ),
        response("end_turn", text_block("done")),
    ]
    svc, fake = _setup(calls)
    with TestClient(create_app(svc)) as client:
        data = client.post("/chat", json={"message": "two reports"}).json()
    assert len(data["tool_calls"]) == 1
    results = fake.messages.calls[1]["messages"][-1]["content"]
    assert results[1]["is_error"] and "Limit" in results[1]["content"]


def test_report_without_stripe_fails_cleanly():
    fake = FakeAnthropic(_report_call())
    svc = build_services(make_settings(), anthropic_client=fake, setup_stripe=False)  # Stripe not configured
    with TestClient(create_app(svc)) as client:
        data = client.post("/chat", json={"message": "report"}).json()
    call = data["tool_calls"][0]
    assert call["status"] == "failed" and "Stripe unavailable" in call["error"]
    assert data["balance_micros"] == 10_000_000
