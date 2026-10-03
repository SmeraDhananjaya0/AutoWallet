"""Pick a rail for each payment and run it against the ledger.

Rules:
  amount >= Stripe minimum ($0.50)  -> Stripe (card)
  amount <  Stripe minimum          -> the configured micropayment rail
                                       (Robinhood Chain by default, Circle as fallback)

A charge is two-phase: the ledger reserves the funds, the rail pays, and the
ledger commits only if the rail succeeded (otherwise the hold is released).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from backend.ledger import InsufficientFunds, Ledger, micros_to_usd
from backend.rails import CircleRail, PaymentResult, Rail, RobinhoodChainRail, StripeRail
from backend.treasury import AutoTopUp

logger = logging.getLogger("autowallet.router")


@dataclass
class ChargeOutcome:
    ok: bool
    amount_micros: int
    rail: str | None = None
    payment: PaymentResult | None = None
    transaction: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class PaymentRouter:
    def __init__(
        self,
        ledger: Ledger,
        *,
        stripe: StripeRail,
        chain: RobinhoodChainRail,
        circle: CircleRail,
        micropayment_rail: str = "robinhood_chain",
        topup: AutoTopUp | None = None,
    ):
        self.ledger = ledger
        self.stripe = stripe
        self.chain = chain
        self.circle = circle
        self.micropayment_rail = micropayment_rail
        self.topup = topup

    def _micro_rails(self) -> list[Rail]:
        order: list[Rail] = [self.chain, self.circle]
        if self.micropayment_rail == "circle":
            order.reverse()
        return order

    def pick_rail(self, amount_micros: int) -> tuple[Rail | None, str]:
        if amount_micros >= self.stripe.min_charge_micros:
            ok, reason = self.stripe.available()
            return (self.stripe, "ready") if ok else (None, f"Stripe unavailable: {reason}")

        reasons = []
        for rail in self._micro_rails():
            ok, reason = rail.available()
            if ok:
                return rail, "ready"
            reasons.append(f"{rail.name}: {reason}")
        return None, "No micropayment rail available (" + "; ".join(reasons) + ")"

    def charge(self, amount_micros: int, reason: str, *, metadata: dict[str, Any] | None = None) -> ChargeOutcome:
        events: list[dict[str, Any]] = []
        if self.topup is not None:
            events.extend(self.topup.before_charge(amount_micros))

        rail, why = self.pick_rail(amount_micros)
        if rail is None:
            return ChargeOutcome(False, amount_micros, events=events, error=why)

        try:
            hold = self.ledger.reserve(amount_micros, reason)
        except InsufficientFunds as exc:
            return ChargeOutcome(False, amount_micros, rail=rail.name, events=events, error=str(exc))

        result = rail.pay(amount_micros, memo=reason)
        if not result.ok:
            self.ledger.release(hold)
            logger.warning("Charge failed on %s: %s", rail.name, result.error)
            return ChargeOutcome(
                False, amount_micros, rail=rail.name, payment=result, events=events, error=result.error
            )

        txn = self.ledger.commit(
            hold,
            rail=rail.name,
            reference=result.reference,
            status=result.status,
            simulated=result.simulated,
            explorer_url=result.explorer_url,
            metadata=metadata or {},
        )
        logger.info(
            "Charged $%.6f via %s (%s) ref=%s", micros_to_usd(amount_micros), rail.name, result.status, result.reference
        )
        return ChargeOutcome(
            True, amount_micros, rail=rail.name, payment=result, transaction=txn.to_dict(), events=events
        )

    def refund(self, outcome: ChargeOutcome, reason: str) -> dict[str, Any] | None:
        """Undo a settled charge when the thing it paid for couldn't be delivered.

        Only card charges can be reversed; on-chain transfers are final.
        Returns the refund transaction, or None if the charge can't be refunded.
        """
        if not outcome.ok or outcome.rail != self.stripe.name or outcome.payment is None:
            return None
        result = self.stripe.refund(outcome.payment.reference, outcome.amount_micros, reason)
        if not result.ok:
            return None
        txn = self.ledger.credit(
            outcome.amount_micros, f"Refund: {reason}", rail=self.stripe.name, reference=result.reference
        )
        return txn.to_dict()

    def status(self) -> list[dict[str, Any]]:
        return [self.chain.status(), self.circle.status(), self.stripe.status()]
