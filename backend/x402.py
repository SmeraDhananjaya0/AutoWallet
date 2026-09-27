"""HTTP 402 paywall for ``POST /search`` (x402-style).

A request without payment gets ``402 Payment Required`` and a list of accepted
payment options. The client retries with an ``X-PAYMENT`` header:

  X-PAYMENT: spt <token>   - spend from a scoped payment token (see spt.py);
                             the server charges its wallet through the router
  X-PAYMENT: tx <hash>     - proof of a USDG transfer to the merchant on
                             Robinhood Chain; verified on-chain, single use

This follows the shape of the x402 protocol (402 + payment requirements +
retry with a payment header) but not its exact wire format or facilitator flow.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse

from backend.ledger import micros_to_usd
from backend.pricing import price_micros

if TYPE_CHECKING:
    from backend.main import Services

SCOPE = "search_web"


@dataclass
class PaymentReceipt:
    scheme: str
    amount_micros: int
    score: int
    rail: str
    reference: str

    def headers(self) -> dict[str, str]:
        return {
            "X-Payment-Scheme": self.scheme,
            "X-Payment-Rail": self.rail,
            "X-Payment-Amount": f"{micros_to_usd(self.amount_micros):.6f}",
            "X-Payment-Reference": self.reference,
            "X-Complexity-Score": str(self.score),
        }


class ReplayGuard:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True


def payment_requirements(services: "Services", query: str, score: int, amount_micros: int) -> dict[str, Any]:
    chain = services.router.chain
    base = services.settings.public_base_url
    return {
        "x402Version": 1,
        "error": "payment_required",
        "resource": f"{base}/search",
        "description": f"Web search, complexity score {score}",
        "amount_usd": micros_to_usd(amount_micros),
        "accepts": [
            {
                "scheme": "spt",
                "header": "X-PAYMENT: spt <token>",
                "issue_url": f"{base}/spt/issue",
                "scope": SCOPE,
            },
            {
                "scheme": "exact",
                "network": f"robinhood-chain:{services.settings.chain_id}",
                "mode": chain.mode,
                "asset": chain.token_address,
                "payTo": chain.merchant_address,
                "maxAmountRequired": str(chain.to_units(amount_micros)),
                "header": "X-PAYMENT: tx <hash>",
            },
        ],
    }


def require_payment(services: "Services", header: str | None, query: str) -> PaymentReceipt | JSONResponse:
    score, amount = price_micros(query)

    def refuse(reason: str | None = None) -> JSONResponse:
        body = payment_requirements(services, query, score, amount)
        if reason:
            body["reason"] = reason
        return JSONResponse(status_code=402, content=body)

    if not header:
        return refuse()

    scheme, _, credential = header.strip().partition(" ")
    scheme = scheme.lower()
    credential = credential.strip()
    if not credential:
        return refuse("malformed X-PAYMENT header")

    if scheme == "spt":
        grant, reason = services.spt.try_spend(credential, SCOPE, amount)
        if grant is None:
            return refuse(reason)
        outcome = services.router.charge(amount, f"x402 search: {query}", metadata={"score": score, "agent": grant.agent_id})
        if not outcome.ok:
            services.spt.refund(grant, amount)
            return refuse(outcome.error)
        return PaymentReceipt("spt", amount, score, outcome.rail or "", outcome.payment.reference)

    if scheme == "tx":
        tx_hash = credential.lower()
        ok, reason = services.router.chain.verify_payment(tx_hash, amount)
        if not ok:
            return refuse(reason)
        # Payments the server made itself (the agent's own charges) aren't valid proof for callers.
        if services.ledger.has_reference(tx_hash) or not services.replay_guard.claim(tx_hash):
            return refuse("transaction already used")
        return PaymentReceipt("tx", amount, score, services.router.chain.name, tx_hash)

    return refuse(f"unsupported payment scheme {scheme!r}")
