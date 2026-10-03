from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import build_services, create_app
from backend.trading import FixedPrices, TradingError
from backend.trading.exchange import SHARE
from tests.conftest import FakeAnthropic, make_settings, response, text_block, tool_use_block


@pytest.fixture
def svc():
    return build_services(make_settings(), setup_stripe=False)


def _propose(svc, **kw):
    args = {"symbol": "NVDA", "side": "buy", "amount_usd": 10, "thesis": "test"} | kw
    return svc.trading.propose(**args)


def test_proposal_does_not_execute(svc):
    p = _propose(svc)
    assert p["status"] == "pending" and p["token"] == "dNVDA"
    assert p["est_shares"] == "0.05"  # $10 at the fixed $200 test price
    assert svc.ledger.balance_micros == 10_000_000
    assert svc.trading.portfolio()["positions"] == []


def test_approve_buys_and_debits_wallet(svc):
    p = _propose(svc)
    done = svc.trading.approve(p["id"])
    assert done["status"] == "executed" and done["fill"]["simulated"]
    assert svc.ledger.balance_micros == 0  # $10 wallet, $10 buy
    [pos] = svc.trading.portfolio()["positions"]
    assert pos["token"] == "dNVDA" and pos["shares"] == "0.05" and pos["value_usd"] == 10.0
    txn = svc.ledger.transactions()[0]
    assert txn["reason"] == "Buy dNVDA" and txn["rail"] == "robinhood_chain" and txn["amount_usd"] == 10.0


def test_cannot_approve_twice_or_after_reject(svc):
    p = _propose(svc, amount_usd=2)
    svc.trading.approve(p["id"])
    with pytest.raises(TradingError, match="executed"):
        svc.trading.approve(p["id"])
    q = _propose(svc, amount_usd=2)
    svc.trading.reject(q["id"])
    with pytest.raises(TradingError, match="rejected"):
        svc.trading.approve(q["id"])


def test_rules_block_proposals(svc):
    assert _propose(svc, amount_usd=30)["status"] == "blocked"  # per-trade cap $25
    svc.trading.set_rules({"allowed_symbols": ["AAPL"]})
    blocked = _propose(svc, amount_usd=5)
    assert blocked["status"] == "blocked"
    assert [c["rule"] for c in blocked["checks"] if not c["ok"]] == ["Allowed ticker"]


def test_kill_switch_blocks_execution_of_already_pending_trade(svc):
    p = _propose(svc, amount_usd=5)
    svc.trading.set_rules({"trading_enabled": False})
    done = svc.trading.approve(p["id"])
    assert done["status"] == "failed" and "kill switch" in done["error"]
    assert svc.ledger.balance_micros == 10_000_000  # nothing spent


def test_daily_limit(svc):
    svc.trading.set_rules({"daily_limit_usd": 12})
    svc.trading.approve(_propose(svc, amount_usd=8)["id"])
    assert _propose(svc, amount_usd=5)["status"] == "blocked"


def test_sell_realizes_pnl(svc):
    svc.trading.approve(_propose(svc, amount_usd=10)["id"])
    svc.trading._prices = FixedPrices({"NVDA": 250.0})  # price up 25%
    assert svc.trading.portfolio()["total_pnl_usd"] == 2.5
    sold = svc.trading.approve(_propose(svc, side="sell", amount_usd=100)["id"])  # capped to holdings
    assert sold["status"] == "executed" and sold["fill"]["amount_usd"] == 12.5
    assert svc.trading.portfolio()["positions"] == []
    assert svc.ledger.balance_micros == 10_000_000 - 10_000_000 + 12_500_000


def test_sell_without_position_is_rejected(svc):
    with pytest.raises(TradingError, match="no dAAPL position"):
        _propose(svc, symbol="AAPL", side="sell")


def test_insufficient_cash_blocks_proposal(svc):
    svc.ledger.set_balance(3_000_000)
    p = _propose(svc, amount_usd=5)
    assert p["status"] == "blocked"
    assert [c["rule"] for c in p["checks"] if not c["ok"]] == ["Enough cash"]


def test_cash_spent_after_proposal_fails_cleanly_at_execution(svc):
    p = _propose(svc, amount_usd=5)
    svc.ledger.set_balance(3_000_000)  # cash dropped between proposal and approval
    done = svc.trading.approve(p["id"])
    assert done["status"] == "failed" and "Enough cash" in done["error"]
    assert svc.ledger.balance_micros == 3_000_000


def test_low_wallet_sells_portfolio_before_robinhood(svc):
    svc.trading.approve(_propose(svc, amount_usd=10)["id"])  # wallet now $0, holding $10 of dNVDA
    outcome = svc.router.charge(4_000, "Web search: x")
    assert outcome.ok
    [event] = outcome.events
    assert event["source"] == "portfolio" and event["status"] == "completed"
    assert event["steps"][0]["step"] == "portfolio_sell"
    # sold enough for threshold ($1) + the charge, not the whole position
    remaining = svc.trading.portfolio()["positions"][0]
    assert 0 < float(remaining["shares"]) < 0.05


def test_portfolio_funding_off_falls_back_to_robinhood(svc):
    svc.trading.approve(_propose(svc, amount_usd=10)["id"])
    svc.trading.set_rules({"fund_from_portfolio": False})
    [event] = svc.router.charge(4_000, "Web search: x").events
    assert event["source"] == "robinhood"


def test_agent_proposes_and_user_approves_via_api():
    fake = FakeAnthropic([
        response("tool_use", tool_use_block("propose_trade", {
            "symbol": "nvda", "side": "buy", "amount_usd": 5, "thesis": "Strong quarter.", "sources": ["https://a.com"]})),
        response("end_turn", text_block("I've proposed buying $5 of dNVDA; approve it in the app.")),
    ])
    svc = build_services(make_settings(), anthropic_client=fake, setup_stripe=False)
    with TestClient(create_app(svc)) as client:
        data = client.post("/api/chat", json={"message": "buy some nvidia"}).json()
        [p] = data["trade_proposals"]
        assert p["status"] == "pending" and data["balance_micros"] == 10_000_000
        tool_result = fake.messages.calls[1]["messages"][-1]["content"][0]["content"]
        assert "NOT been executed" in tool_result

        approved = client.post(f"/api/trades/{p['id']}/approve").json()
        assert approved["proposal"]["status"] == "executed"
        assert approved["balance_micros"] == 5_000_000
        assert approved["portfolio"]["positions"][0]["token"] == "dNVDA"
        assert client.post(f"/api/trades/{p['id']}/approve").status_code == 409


def test_rules_api_validates(client):
    assert client.put("/api/trading/rules", json={"max_trade_usd": -1}).status_code == 400
    assert client.put("/api/trading/rules", json={"trading_enabled": False}).json()["trading_enabled"] is False
