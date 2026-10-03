"""Auto top-up: keep the agent wallet funded from a Robinhood-backed treasury.

Flow when a charge would leave the balance under the threshold:
  1. Buy the top-up amount of a stablecoin on Robinhood (Crypto Trading API).
  2. Send the same amount of USDG from the treasury wallet to the agent wallet
     on Robinhood Chain.
  3. Credit the ledger only after both steps succeed.

Why a treasury wallet? The Crypto Trading API has no withdrawal endpoint, so
coins bought on Robinhood can't be pushed on-chain programmatically. Step 1
replenishes the Robinhood position; you periodically withdraw from Robinhood to
the treasury address in the app. Step 2 is what the agent actually spends.

Guardrails: a per-event amount, a rolling 24h cap (read from the ledger), and a lock so concurrent
charges can't trigger duplicate top-ups.
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from typing import Any, Callable

from backend.config import Settings
from backend.ledger import Ledger, micros_to_usd, usd_to_micros, utc_now_iso
from backend.rails.robinhood_chain import RobinhoodChainRail
from backend.treasury.robinhood_crypto import CryptoTreasury

logger = logging.getLogger("autowallet.treasury.auto_topup")

TOPUP_RAIL = "robinhood"


class AutoTopUp:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        crypto: CryptoTreasury,
        chain: RobinhoodChainRail,
        portfolio_funder: Callable[[int], dict[str, Any] | None] | None = None,
    ):
        self._s = settings
        self._ledger = ledger
        self._crypto = crypto
        self._chain = chain
        # Optional: sell from the agent's demo portfolio before tapping the Robinhood treasury.
        self.portfolio_funder = portfolio_funder
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.enabled = settings.auto_topup_enabled
        self.threshold_micros = usd_to_micros(settings.auto_topup_threshold_usd)
        self.amount_micros = usd_to_micros(settings.auto_topup_amount_usd)
        self.daily_cap_micros = usd_to_micros(settings.auto_topup_daily_cap_usd)

    def spent_last_24h_micros(self) -> int:
        # Derived from the ledger, so the cap holds across server restarts.
        return self._ledger.credited_since(TOPUP_RAIL, hours=24)

    def before_charge(self, amount_micros: int) -> list[dict[str, Any]]:
        """Top up if paying ``amount_micros`` would drop the balance under the threshold."""
        if not self.enabled:
            return []
        with self._lock:
            balance = self._ledger.balance_micros
            if balance - amount_micros >= self.threshold_micros:
                return []
            shortfall = amount_micros + self.threshold_micros - balance
            return [self._run(max(self.amount_micros, shortfall), trigger="low_balance")]

    def run_now(self, amount_micros: int | None = None) -> dict[str, Any]:
        with self._lock:
            return self._run(amount_micros or self.amount_micros, trigger="manual")

    def _run(self, amount_micros: int, *, trigger: str) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": "auto_topup",
            "trigger": trigger,
            "timestamp": utc_now_iso(),
            "amount_usd": micros_to_usd(amount_micros),
            "status": "pending",
            "steps": [],
        }
        self.events.insert(0, event)

        # 0. Self-funding: sell part of the agent's portfolio first (credits the ledger itself).
        if self.portfolio_funder is not None:
            try:
                sold = self.portfolio_funder(amount_micros)
            except Exception as exc:
                logger.warning("Portfolio funding failed, falling back to Robinhood: %s", exc)
                event["steps"].append({"step": "portfolio_sell", "ok": False, "error": str(exc)})
                sold = None
            if sold:
                last_tx = sold["txs"][-1] if sold["txs"] else {}
                event.update(
                    source="portfolio",
                    amount_usd=micros_to_usd(sold["amount_micros"]),
                    status="completed",
                    transaction_id=sold["transaction_id"],
                )
                event["steps"].append(
                    {
                        "step": "portfolio_sell",
                        "ok": True,
                        "symbol": sold["symbol"],
                        "detail": f"Sold {sold['shares']} {sold['token']} @ ${sold['price_usd']:,.2f}",
                        "tx_hash": last_tx.get("hash"),
                        "explorer_url": last_tx.get("explorer_url"),
                        "simulated": sold["simulated"],
                    }
                )
                logger.info("Auto top-up funded from portfolio: %s", event["steps"][-1]["detail"])
                return event

        event["source"] = "robinhood"
        remaining = self.daily_cap_micros - self.spent_last_24h_micros()
        if amount_micros > remaining:
            event["status"] = "blocked"
            event["error"] = (
                f"Daily auto top-up cap reached (${micros_to_usd(self.daily_cap_micros):.2f}/24h, "
                f"${micros_to_usd(max(remaining, 0)):.2f} left)"
            )
            logger.warning(event["error"])
            return event

        # 1. Robinhood buy
        symbol = self._s.crypto_topup_symbol
        try:
            order = self._crypto.buy_usd_amount(symbol, Decimal(amount_micros) / Decimal(1_000_000))
        except Exception as exc:
            event["status"] = "failed"
            event["error"] = f"Robinhood buy failed: {exc}"
            event["steps"].append({"step": "robinhood_buy", "ok": False, "error": str(exc)})
            logger.warning(event["error"])
            return event
        order_state = str(order.get("state", "")).lower()
        buy_ok = order_state == "filled"
        event["steps"].append(
            {
                "step": "robinhood_buy",
                "ok": buy_ok,
                "symbol": symbol,
                "order_id": order.get("id"),
                "state": order_state,
                "simulated": self._crypto.simulated,
            }
        )
        if not buy_ok:
            event["status"] = "failed"
            event["error"] = f"Robinhood order not filled (state={order_state or 'unknown'})"
            return event

        # 2. Treasury -> agent on Robinhood Chain
        transfer = self._chain.fund_agent_from_treasury(amount_micros, memo=f"Auto top-up ({trigger})")
        event["steps"].append(
            {
                "step": "robinhood_chain_transfer",
                "ok": transfer.ok,
                "tx_hash": transfer.reference,
                "explorer_url": transfer.explorer_url,
                "simulated": transfer.simulated,
                "error": transfer.error,
            }
        )
        if not transfer.ok:
            event["status"] = "failed"
            event["error"] = f"Treasury transfer failed: {transfer.error}"
            return event

        # 3. Credit the ledger
        txn = self._ledger.credit(
            amount_micros,
            "Auto top-up via Robinhood",
            rail=TOPUP_RAIL,
            reference=transfer.reference,
            simulated=transfer.simulated or self._crypto.simulated,
            explorer_url=transfer.explorer_url,
            metadata={"robinhood_order_id": order.get("id"), "symbol": symbol, "trigger": trigger},
        )
        event["status"] = "completed"
        event["transaction_id"] = txn.id
        logger.info("Auto top-up completed: $%.2f (%s)", micros_to_usd(amount_micros), trigger)
        return event

    def status(self) -> dict[str, Any]:
        spent = self.spent_last_24h_micros()
        return {
            "enabled": self.enabled,
            "threshold_usd": micros_to_usd(self.threshold_micros),
            "amount_usd": micros_to_usd(self.amount_micros),
            "daily_cap_usd": micros_to_usd(self.daily_cap_micros),
            "spent_last_24h_usd": micros_to_usd(spent),
            "symbol": self._s.crypto_topup_symbol,
            "robinhood": self._crypto.status(),
            "recent_events": self.events[:5],
        }
