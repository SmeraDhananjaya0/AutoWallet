from __future__ import annotations

from unittest.mock import MagicMock

from backend.ledger import usd_to_micros
from backend.main import build_services
from backend.rails import FAILED, SETTLED, PaymentResult
from backend.treasury import SimulatedRobinhoodCrypto
from tests.conftest import make_settings


def test_micropayment_goes_to_robinhood_chain(services):
    outcome = services.router.charge(1_000, "Web search: hi")
    assert outcome.ok
    assert outcome.rail == "robinhood_chain"
    assert outcome.payment.simulated
    assert outcome.payment.reference.startswith("0x")
    assert services.ledger.balance_micros == usd_to_micros(10) - 1_000


def test_charge_at_stripe_minimum_goes_to_stripe(services):
    stripe = services.router.stripe
    stripe.customer_id = "cus_test"
    stripe.charge_card = MagicMock(
        return_value=PaymentResult("stripe", 500_000, SETTLED, reference="ch_test")
    )
    outcome = services.router.charge(500_000, "big purchase")
    assert outcome.ok and outcome.rail == "stripe"
    stripe.charge_card.assert_called_once()


def test_stripe_refuses_below_minimum(services):
    result = services.router.stripe.pay(10_000, "one cent")
    assert result.status == FAILED
    assert "minimum" in result.error


def test_failed_rail_releases_hold(services):
    services.router.chain.pay = MagicMock(
        return_value=PaymentResult("robinhood_chain", 1_000, FAILED, error="boom")
    )
    before = services.ledger.balance_micros
    outcome = services.router.charge(1_000, "search")
    assert not outcome.ok and outcome.error == "boom"
    assert services.ledger.balance_micros == before
    assert services.ledger.transactions() == []


def test_falls_back_to_circle_when_chain_unavailable(services):
    services.router.chain.available = lambda: (False, "down")
    services.router.circle.available = lambda: (True, "configured")
    services.router.circle.pay = MagicMock(
        return_value=PaymentResult("circle", 1_000, "submitted", reference="circle-tx")
    )
    outcome = services.router.charge(1_000, "search")
    assert outcome.ok and outcome.rail == "circle"


def test_no_rail_means_no_debit(services):
    services.router.chain.available = lambda: (False, "down")
    before = services.ledger.balance_micros
    outcome = services.router.charge(1_000, "search")
    assert not outcome.ok
    assert "No micropayment rail" in outcome.error
    assert services.ledger.balance_micros == before


# --- auto top-up ---------------------------------------------------------------


def test_low_balance_triggers_robinhood_topup_before_charge(services):
    services.ledger.set_balance(2_000)  # $0.002, under the $1 threshold
    outcome = services.router.charge(5_000, "Web search: something complex")

    assert outcome.ok
    [event] = outcome.events
    assert event["type"] == "auto_topup" and event["status"] == "completed"
    assert [s["step"] for s in event["steps"]] == ["robinhood_buy", "robinhood_chain_transfer"]
    # +$5 top-up, -$0.005 charge
    assert services.ledger.balance_micros == 2_000 + 5_000_000 - 5_000
    kinds = [t["kind"] for t in services.ledger.transactions()]
    assert kinds == ["charge", "topup"]


def test_no_topup_when_balance_is_healthy(services):
    outcome = services.router.charge(1_000, "search")
    assert outcome.events == []


def test_robinhood_failure_does_not_credit(settings):
    svc = build_services(settings, setup_stripe=False)
    svc.topup._crypto = SimulatedRobinhoodCrypto(fail=True)
    svc.ledger.set_balance(0)
    outcome = svc.router.charge(1_000, "search")
    assert not outcome.ok
    assert outcome.events[0]["status"] == "failed"
    assert svc.ledger.balance_micros == 0


def test_daily_cap_blocks_topups():
    svc = build_services(make_settings(auto_topup_daily_cap_usd=6.0), setup_stripe=False)
    assert svc.topup.run_now()["status"] == "completed"  # $5 of $6
    assert svc.topup.run_now()["status"] == "blocked"


def test_live_chain_without_keys_is_unavailable():
    svc = build_services(make_settings(chain_mode="mainnet"), setup_stripe=False)
    ok, reason = svc.router.chain.available()
    assert not ok and "AGENT_WALLET_PRIVATE_KEY" in reason
