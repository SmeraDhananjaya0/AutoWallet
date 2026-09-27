from __future__ import annotations

from fastapi.testclient import TestClient

from backend.ledger import Ledger
from backend.main import build_services, create_app
from tests.conftest import make_settings


def test_ledger_survives_restart(tmp_path):
    db = tmp_path / "ledger.db"
    first = Ledger(10_000_000, db_path=db)
    hold = first.reserve(4_000, "search")
    first.commit(hold, rail="robinhood_chain", reference="0xabc")
    first.credit(5_000_000, "top-up", rail="robinhood")

    second = Ledger(999, db_path=db)  # initial balance ignored once the DB exists
    assert second.balance_micros == 10_000_000 - 4_000 + 5_000_000
    assert [t["kind"] for t in second.transactions()] == ["topup", "charge"]
    assert second.has_reference("0xABC")


def test_uncommitted_hold_is_not_persisted_as_spent(tmp_path):
    db = tmp_path / "ledger.db"
    first = Ledger(1_000_000, db_path=db)
    first.reserve(500_000, "in flight")
    first.credit(1, "nudge", rail="stripe")  # forces a write while the hold is open
    assert Ledger(0, db_path=db).balance_micros == 1_000_001


def test_topup_daily_cap_survives_restart(tmp_path):
    settings = make_settings(ledger_db_path=str(tmp_path / "ledger.db"), auto_topup_daily_cap_usd=6.0)
    assert build_services(settings, setup_stripe=False).topup.run_now()["status"] == "completed"
    restarted = build_services(settings, setup_stripe=False)
    assert restarted.topup.spent_last_24h_micros() == 5_000_000
    assert restarted.topup.run_now()["status"] == "blocked"


def _issue(client, headers=None):
    return client.post(
        "/spt/issue", json={"agent_id": "a", "max_amount_usd": 0.01}, headers=headers or {}
    )


def test_issue_requires_key_when_configured():
    svc = build_services(make_settings(spt_issuer_key="issuer-key-123"), setup_stripe=False)
    with TestClient(create_app(svc)) as client:
        assert _issue(client).status_code == 401
        assert _issue(client, {"Authorization": "Bearer wrong"}).status_code == 401
        assert _issue(client, {"Authorization": "Bearer issuer-key-123"}).status_code == 200


def test_issue_closed_outside_demo_mode_without_key():
    svc = build_services(make_settings(demo_mode=False), setup_stripe=False)
    with TestClient(create_app(svc)) as client:
        assert _issue(client).status_code == 403
        assert client.post("/demo/set-balance", json={"balance_usd": 1}).status_code == 404
