"""Stripe card rail: card top-ups and payments of at least the Stripe minimum ($0.50)."""

from __future__ import annotations

import logging
from typing import Any

import stripe

from backend.config import Settings
from backend.ledger import micros_to_usd, usd_to_micros
from backend.rails.base import FAILED, SETTLED, PaymentResult

logger = logging.getLogger("autowallet.rails.stripe")

TEST_PAYMENT_METHOD = "pm_card_visa"
MICROS_PER_CENT = 10_000


class StripeRail:
    name = "stripe"

    def __init__(self, settings: Settings):
        self._settings = settings
        self.customer_id: str | None = None
        self._setup_error: str | None = None
        self.min_charge_micros = usd_to_micros(settings.stripe_min_charge_usd)

    def setup(self) -> None:
        key = self._settings.stripe_secret_key
        if not key:
            self._setup_error = "STRIPE_SECRET_KEY not set"
            return
        if key.startswith("sk_live") and not self._settings.stripe_allow_live_keys:
            # The rail charges a hard-coded test card; never point it at a live account by accident.
            self._setup_error = "Live Stripe key refused (set STRIPE_ALLOW_LIVE_KEYS=true to override)"
            return
        stripe.api_key = key
        try:
            customer = stripe.Customer.create(name="AutoWallet Agent")
        except stripe.StripeError as exc:
            self._setup_error = f"Stripe customer creation failed: {exc.user_message or exc}"
            logger.warning(self._setup_error)
            return
        self.customer_id = customer.id
        logger.info("Stripe customer created: %s", customer.id)

    def available(self) -> tuple[bool, str]:
        if self.customer_id:
            return True, "ready"
        return False, self._setup_error or "not initialized"

    def pay(self, amount_micros: int, memo: str) -> PaymentResult:
        if amount_micros < self.min_charge_micros:
            return PaymentResult(
                rail=self.name,
                amount_micros=amount_micros,
                status=FAILED,
                error=(
                    f"${micros_to_usd(amount_micros):.6f} is below the Stripe minimum of "
                    f"${micros_to_usd(self.min_charge_micros):.2f}"
                ),
            )
        return self.charge_card(amount_micros, memo)

    def charge_card(self, amount_micros: int, memo: str) -> PaymentResult:
        ok, reason = self.available()
        if not ok:
            return PaymentResult(self.name, amount_micros, FAILED, error=reason)
        if amount_micros % MICROS_PER_CENT:
            return PaymentResult(
                self.name, amount_micros, FAILED, error="Stripe amounts must be whole cents"
            )
        try:
            intent = stripe.PaymentIntent.create(
                amount=amount_micros // MICROS_PER_CENT,
                currency="usd",
                customer=self.customer_id,
                payment_method=TEST_PAYMENT_METHOD,
                confirm=True,
                off_session=True,
                description=memo[:500],
            )
        except stripe.StripeError as exc:
            logger.warning("Stripe charge failed: %s", exc.user_message or exc)
            return PaymentResult(
                self.name, amount_micros, FAILED, error=str(exc.user_message or exc)
            )
        return PaymentResult(
            rail=self.name,
            amount_micros=amount_micros,
            status=SETTLED,
            reference=_charge_id(intent),
        )

    def status(self) -> dict[str, Any]:
        ok, reason = self.available()
        return {
            "rail": self.name,
            "available": ok,
            "detail": reason,
            "min_charge_usd": micros_to_usd(self.min_charge_micros),
        }


def _charge_id(intent: Any) -> str:
    charge = getattr(intent, "latest_charge", None)
    if charge is None:
        return intent.id
    if isinstance(charge, str):
        return charge
    return getattr(charge, "id", str(charge))
