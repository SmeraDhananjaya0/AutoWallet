"""Tests for complexity scoring, payment routing, and x402 middleware."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from complexity_scorer import score_query, score_to_price
from payment_router import decide_query_rail, route_payment
from x402_middleware import X402PaymentMiddleware


# --- complexity_scorer ---


def test_score_query_hi_returns_1():
    assert score_query("hi") == 1


def test_score_query_compare_returns_at_least_4():
    assert score_query("compare GPT-4 vs Claude for coding") >= 4


def test_score_query_latest_research_returns_at_least_4():
    assert score_query("what are the latest AI research developments this week?") >= 4


def test_score_to_price_score_1():
    assert score_to_price(1) == 0.001


def test_score_to_price_score_10():
    assert score_to_price(10) == 0.01


@pytest.mark.parametrize("score", range(1, 11))
def test_score_to_price_rounded_to_four_decimal_places(score: int):
    price = score_to_price(score)
    assert isinstance(price, float)
    assert price == round(score * 0.001, 4)


# --- payment_router ---


def test_decide_query_rail_simple_query_uses_circle():
    decision = decide_query_rail("hi")
    assert decision["score"] == 1
    assert decision["total_cost_usd"] == 0.001
    assert decision["rail"] == "circle"


def test_decide_query_rail_score_10_uses_stripe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("complexity_scorer.score_query", lambda _q: 10)
    decision = decide_query_rail("synthetic complex query")
    assert decision["total_cost_usd"] == 0.01
    assert decision["rail"] == "stripe"


def test_decide_query_rail_score_9_stays_circle(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("complexity_scorer.score_query", lambda _q: 9)
    decision = decide_query_rail("synthetic query")
    assert decision["total_cost_usd"] == 0.009
    assert decision["rail"] == "circle"


@pytest.fixture
def no_circle_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CIRCLE_API_KEY", raising=False)


@patch("payment_router.httpx.post")
def test_route_payment_0001_circle_rail(mock_post: MagicMock, no_circle_key: None):
    result = route_payment(0.001)
    assert result["rail"] == "circle"
    mock_post.assert_not_called()


@patch("payment_router.httpx.post")
def test_route_payment_0009_circle_rail(mock_post: MagicMock, no_circle_key: None):
    result = route_payment(0.009)
    assert result["rail"] == "circle"
    mock_post.assert_not_called()


@patch("payment_router.charge_wallet")
@patch("payment_router.httpx.post")
def test_route_payment_001_stripe_rail(
    mock_post: MagicMock,
    mock_charge: MagicMock,
    no_circle_key: None,
):
    mock_charge.return_value = (
        "Successfully charged $0.01 (1 cents) for: Payment router: $0.0100.",
        [{"stripe_charge_id": "pi_test_001"}],
    )
    result = route_payment(0.01)
    assert result["rail"] == "stripe"
    mock_post.assert_not_called()
    mock_charge.assert_called_once()


@patch("payment_router.charge_wallet")
@patch("payment_router.httpx.post")
def test_route_payment_080_stripe_rail(
    mock_post: MagicMock,
    mock_charge: MagicMock,
    no_circle_key: None,
):
    mock_charge.return_value = (
        "Successfully charged $0.80 (80 cents) for: Payment router: $0.8000.",
        [{"stripe_charge_id": "pi_test_080"}],
    )
    result = route_payment(0.80)
    assert result["rail"] == "stripe"
    mock_post.assert_not_called()
    mock_charge.assert_called_once()


@patch("payment_router.httpx.post")
def test_route_payment_circle_skipped_without_api_key(
    mock_post: MagicMock,
    no_circle_key: None,
):
    result = route_payment(0.005)
    assert result["rail"] == "circle"
    assert result["status"] == "skipped"
    assert result["status"] != "failed"
    mock_post.assert_not_called()


# --- X402PaymentMiddleware ---


@pytest.fixture
def search_app():
    received: list[str] = []

    app = FastAPI()

    @app.post("/search")
    async def search(request: Request) -> dict:
        body = await request.json()
        query = body.get("query", "")
        received.append(query)
        return {"ok": True, "query": query}

    app.state.received_queries = received
    app.add_middleware(X402PaymentMiddleware)
    return app


@pytest.fixture
def search_client(search_app: FastAPI):
    with patch("payment_router.route_payment") as mock_route:
        mock_route.return_value = {
            "rail": "circle",
            "amount_usd": 0.001,
            "status": "skipped",
            "reason": "CIRCLE_API_KEY not set",
        }
        with TestClient(search_app) as client:
            client.mock_route_payment = mock_route
            yield client


def test_x402_hi_returns_200_with_payment_rail_header(search_client: TestClient):
    response = search_client.post("/search", json={"query": "hi"})
    assert response.status_code == 200
    assert "x-payment-rail" in response.headers
    assert response.json() == {"ok": True, "query": "hi"}


@patch("payment_router.route_payment")
def test_x402_compare_query_payment_rail(mock_route: MagicMock, search_app: FastAPI):
    score = score_query("compare GPT-4 vs Claude")
    amount_usd = score_to_price(score)
    expected_rail = "circle" if amount_usd < 0.01 else "stripe"
    mock_route.return_value = {
        "rail": expected_rail,
        "amount_usd": amount_usd,
        "status": "skipped",
    }

    with TestClient(search_app) as client:
        response = client.post("/search", json={"query": "compare GPT-4 vs Claude"})

    assert response.status_code == 200
    assert response.headers["x-payment-rail"] == expected_rail


def test_x402_downstream_receives_full_request_body(search_client: TestClient):
    query = "compare GPT-4 vs Claude in depth for a research paper"
    response = search_client.post("/search", json={"query": query})

    assert response.status_code == 200
    assert response.json()["query"] == query
    assert search_client.app.state.received_queries[-1] == query
