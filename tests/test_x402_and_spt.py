from __future__ import annotations

import time

import jwt

from backend.spt import SptIssuer

SECRET = "unit-test-secret-0123456789abcdef0123"


def _issue(client, max_amount_usd=0.05):
    resp = client.post("/spt/issue", json={"agent_id": "a1", "tool_scope": "search_web", "max_amount_usd": max_amount_usd})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def test_search_without_payment_returns_402_with_requirements(client):
    resp = client.post("/search", json={"query": "hi"})
    assert resp.status_code == 402
    body = resp.json()
    schemes = {a["scheme"] for a in body["accepts"]}
    assert schemes == {"spt", "exact"}
    assert body["amount_usd"] == 0.001


def test_search_with_spt_charges_wallet_and_returns_results(client, services):
    token = _issue(client)
    before = services.ledger.balance_micros
    resp = client.post("/search", json={"query": "hi"}, headers={"X-PAYMENT": f"spt {token}"})
    assert resp.status_code == 200
    assert resp.headers["x-payment-rail"] == "robinhood_chain"
    assert resp.headers["x-payment-amount"] == "0.001000"
    assert resp.json()["results"]
    assert services.ledger.balance_micros == before - 1_000


def test_spt_allowance_is_enforced_across_calls(client):
    token = _issue(client, max_amount_usd=0.0015)
    ok = client.post("/search", json={"query": "hi"}, headers={"X-PAYMENT": f"spt {token}"})
    assert ok.status_code == 200
    spent = client.post("/search", json={"query": "hi"}, headers={"X-PAYMENT": f"spt {token}"})
    assert spent.status_code == 402
    assert "allowance exhausted" in spent.json()["reason"]


def test_spt_cap_on_issue(client):
    resp = client.post("/spt/issue", json={"agent_id": "a1", "max_amount_usd": 5})
    assert resp.status_code == 400


def test_spt_wrong_scope_and_expiry():
    issuer = SptIssuer(SECRET, 1.0)
    token = issuer.issue("a1", "other_tool", 0.5)
    assert issuer.try_spend(token, "search_web", 1_000)[0] is None

    expired = jwt.encode({"jti": "x", "scope": "search_web", "exp": int(time.time()) - 10}, SECRET, algorithm="HS256")
    grant, reason = issuer.lookup(expired, "search_web")
    assert grant is None and reason == "token expired"


def test_spt_not_signed_with_stripe_key():
    issuer = SptIssuer("spt-only-secret-0123456789abcdef0123", 1.0)
    token = issuer.issue("a1", "search_web", 0.5)
    assert jwt.decode(token, "spt-only-secret-0123456789abcdef0123", algorithms=["HS256"])["scope"] == "search_web"


def test_failed_charge_refunds_spt_allowance(client, services):
    services.router.chain.available = lambda: (False, "down")
    token = _issue(client, max_amount_usd=0.001)
    resp = client.post("/search", json={"query": "hi"}, headers={"X-PAYMENT": f"spt {token}"})
    assert resp.status_code == 402
    services.router.chain.available = lambda: (True, "simulated")
    retry = client.post("/search", json={"query": "hi"}, headers={"X-PAYMENT": f"spt {token}"})
    assert retry.status_code == 200


def test_tx_proof_accepted_once(client):
    tx = client.post("/demo/external-payment", json={"amount_usd": 0.01}).json()["tx_hash"]
    first = client.post("/search", json={"query": "hi"}, headers={"X-PAYMENT": f"tx {tx}"})
    assert first.status_code == 200
    assert first.headers["x-payment-scheme"] == "tx"
    replay = client.post("/search", json={"query": "hi"}, headers={"X-PAYMENT": f"tx {tx}"})
    assert replay.status_code == 402


def test_agents_own_payment_is_not_valid_proof(client, services):
    outcome = services.router.charge(10_000, "agent search")
    resp = client.post("/search", json={"query": "hi"}, headers={"X-PAYMENT": f"tx {outcome.payment.reference}"})
    assert resp.status_code == 402


def test_unknown_scheme_is_402(client):
    resp = client.post("/search", json={"query": "hi"}, headers={"X-PAYMENT": "cash 5"})
    assert resp.status_code == 402


def test_manifest_and_health(client):
    manifest = client.get("/merchant/manifest").json()
    assert manifest["payment"]["schemes"][1]["network"].startswith("robinhood-chain:")
    health = client.get("/health").json()
    assert health["rails"]["robinhood_chain"] is True
    assert health["rails"]["stripe"] is False
