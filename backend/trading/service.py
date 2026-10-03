"""Demo trading: research -> proposal -> human approval -> on-chain trade.

Testnet and play money only. The agent can *propose* trades; only an explicit
approval (the user clicking Approve, i.e. POST /trades/{id}/approve) executes one.
Every proposal and every execution is checked against the trading rules.

Money flow: buys spend from the agent's wallet budget (the ledger) and settle as
a tUSDG -> demo-stock-token swap on Robinhood Chain testnet; sells credit the
budget. Positions track shares and cost basis for P&L.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.ledger import InsufficientFunds, micros_to_usd, usd_to_micros
from backend.trading import policy
from backend.trading.exchange import SHARE, DemoExchange, ExchangeError
from backend.trading.policy import TradingRules
from backend.trading.prices import PriceError, normalize_symbol

logger = logging.getLogger("autowallet.trading")

TRADE_RAIL = "robinhood_chain"
MIN_TRADE_MICROS = 1_000_000  # $1


class TradingError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def shares_str(units: int) -> str:
    return f"{units / SHARE:.6f}".rstrip("0").rstrip(".")


class TradingService:
    def __init__(self, ledger: Any, prices: Any, exchange: DemoExchange, store: Any):
        self._ledger = ledger
        self._prices = prices
        self._exchange = exchange
        self._store = store

    # --- rules ----------------------------------------------------------

    def rules(self) -> TradingRules:
        return TradingRules.from_dict(self._store.get_rules())

    def set_rules(self, data: dict[str, Any]) -> TradingRules:
        rules = TradingRules.from_dict({**self.rules().to_dict(), **data})
        self._store.set_rules(rules.to_dict())
        return rules

    # --- market data ----------------------------------------------------

    def quote(self, symbol: str) -> dict[str, Any]:
        q = self._prices.quote(symbol)
        return {**q.to_dict(), "token": f"d{q.symbol}", "token_url": self._exchange.token_explorer_url(q.symbol)}

    def quotes(self) -> list[dict[str, Any]]:
        out = []
        for s in self._prices.symbols():
            try:
                out.append(self.quote(s))
            except PriceError as exc:
                out.append({"symbol": s, "error": str(exc)})
        return out

    def portfolio(self) -> dict[str, Any]:
        rows, total_value, total_cost = [], 0, 0
        for symbol, pos in sorted(self._store.positions().items()):
            try:
                price = self._prices.quote(symbol).price_micros
            except PriceError:
                price = None
            value = pos["shares_units"] * price // SHARE if price else None
            rows.append(
                {
                    "symbol": symbol,
                    "token": f"d{symbol}",
                    "shares": shares_str(pos["shares_units"]),
                    "price_usd": micros_to_usd(price) if price else None,
                    "value_usd": micros_to_usd(value) if value is not None else None,
                    "cost_usd": micros_to_usd(pos["cost_micros"]),
                    "pnl_usd": micros_to_usd(value - pos["cost_micros"]) if value is not None else None,
                    "token_url": self._exchange.token_explorer_url(symbol),
                }
            )
            total_value += value or 0
            total_cost += pos["cost_micros"]
        return {
            "positions": rows,
            "total_value_usd": micros_to_usd(total_value),
            "total_cost_usd": micros_to_usd(total_cost),
            "total_pnl_usd": micros_to_usd(total_value - total_cost),
            "cash_usd": micros_to_usd(self._ledger.balance_micros),
            "exchange": self._exchange.status(),
            "rules": self.rules().to_dict(),
        }

    # --- proposals ------------------------------------------------------

    def propose(
        self,
        *,
        symbol: str,
        side: str,
        amount_usd: float,
        thesis: str,
        sources: list[str] | None = None,
        research_cost_micros: int = 0,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        side = side.lower()
        if side not in ("buy", "sell"):
            raise TradingError("side must be 'buy' or 'sell'")
        symbol = normalize_symbol(symbol)
        quote = self._prices.quote(symbol)  # raises PriceError for unknown symbols
        usd = usd_to_micros(amount_usd)
        if usd < MIN_TRADE_MICROS:
            raise TradingError("minimum trade is $1.00")

        shares = usd * SHARE // quote.price_micros
        if side == "sell":
            held = self._store.positions().get(symbol, {}).get("shares_units", 0)
            if held <= 0:
                raise TradingError(f"no d{symbol} position to sell")
            shares = min(shares, held)
            usd = shares * quote.price_micros // SHARE

        checks = policy.check(
            self.rules(),
            symbol=symbol,
            usd_micros=usd,
            executed_today_micros=self._store.executed_notional_since(24),
            cash_micros=self._ledger.balance_micros if side == "buy" else None,
        )
        proposal = {
            "id": f"trd_{uuid.uuid4().hex[:10]}",
            "created_at": _now_iso(),
            "session_id": session_id,
            "symbol": symbol,
            "token": f"d{symbol}",
            "name": quote.name,
            "side": side,
            "usd_micros": usd,
            "amount_usd": micros_to_usd(usd),
            "shares_units": shares,
            "est_shares": shares_str(shares),
            "price_usd": micros_to_usd(quote.price_micros),
            "price_source": quote.source,
            "thesis": thesis.strip(),
            "sources": [s for s in (sources or []) if isinstance(s, str)][:8],
            "research_cost_usd": micros_to_usd(research_cost_micros),
            "checks": checks,
            "status": "pending" if policy.passed(checks) else "blocked",
            "network": "Robinhood Chain testnet (play money)" if not self._exchange.simulated else "simulated",
        }
        self._store.insert_proposal(proposal)
        logger.info("trade proposal %s: %s %s $%.2f -> %s", proposal["id"], side, symbol, micros_to_usd(usd), proposal["status"])
        return proposal

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._store.list_proposals(limit)

    def reject(self, pid: str) -> dict[str, Any]:
        if not self._store.transition(pid, "pending", "rejected"):
            raise TradingError(self._not_pending(pid))
        return self._store.update_proposal(pid, decided_at=_now_iso())

    def approve(self, pid: str) -> dict[str, Any]:
        """Execute a pending proposal. Rules and price are re-checked at execution time."""
        if not self._store.transition(pid, "pending", "executing"):
            raise TradingError(self._not_pending(pid))
        p = self._store.get_proposal(pid)
        try:
            fill_data = self._execute(p)
        except (TradingError, ExchangeError, PriceError, InsufficientFunds) as exc:
            logger.warning("trade %s failed: %s", pid, exc)
            return self._store.update_proposal(pid, status="failed", error=str(exc), decided_at=_now_iso())
        except Exception as exc:  # never leave a proposal stuck in 'executing'
            logger.exception("trade %s failed unexpectedly", pid)
            return self._store.update_proposal(pid, status="failed", error=str(exc), decided_at=_now_iso())
        return self._store.update_proposal(pid, status="executed", fill=fill_data, decided_at=_now_iso())

    def _execute(self, p: dict[str, Any]) -> dict[str, Any]:
        symbol, side = p["symbol"], p["side"]
        quote = self._prices.quote(symbol)
        rules = self.rules()
        usd_now = p["usd_micros"] if side == "buy" else p["shares_units"] * quote.price_micros // SHARE
        checks = policy.check(
            rules,
            symbol=symbol,
            usd_micros=usd_now,
            executed_today_micros=self._store.executed_notional_since(24),
            cash_micros=self._ledger.balance_micros if side == "buy" else None,
        )
        if not policy.passed(checks):
            failed = "; ".join(f"{c['rule']}: {c['detail']}" for c in checks if not c["ok"])
            raise TradingError(f"blocked by trading rules at execution: {failed}")

        if side == "buy":
            hold = self._ledger.reserve(p["usd_micros"], f"Buy d{symbol}")
            try:
                fill = self._exchange.buy(symbol, p["usd_micros"], quote)
            except Exception:
                self._ledger.release(hold)
                raise
            self._ledger.commit(
                hold,
                rail=TRADE_RAIL,
                reference=fill.reference,
                simulated=fill.simulated,
                explorer_url=fill.explorer_url,
                metadata={"trade_id": p["id"], "symbol": symbol, "shares": shares_str(fill.shares_units)},
            )
            self._store.apply_fill(symbol, fill.shares_units, fill.usd_micros)
        else:
            pos = self._store.positions().get(symbol)
            if not pos or pos["shares_units"] <= 0:
                raise TradingError(f"no d{symbol} position to sell")
            shares = min(p["shares_units"], pos["shares_units"])
            fill = self._exchange.sell(symbol, shares, quote)
            cost_removed = pos["cost_micros"] * fill.shares_units // pos["shares_units"]
            self._store.apply_fill(symbol, -fill.shares_units, -cost_removed)
            self._ledger.credit(
                fill.usd_micros,
                f"Sell d{symbol}",
                rail=TRADE_RAIL,
                reference=fill.reference,
                simulated=fill.simulated,
                explorer_url=fill.explorer_url,
                metadata={"trade_id": p["id"], "symbol": symbol, "shares": shares_str(fill.shares_units)},
            )

        return {
            "usd_micros": fill.usd_micros,
            "amount_usd": micros_to_usd(fill.usd_micros),
            "shares": shares_str(fill.shares_units),
            "price_usd": micros_to_usd(fill.price_micros),
            "simulated": fill.simulated,
            "txs": fill.txs,
        }

    def _not_pending(self, pid: str) -> str:
        p = self._store.get_proposal(pid)
        return "unknown trade proposal" if p is None else f"proposal is {p['status']}, not pending"

    # --- self-funding ---------------------------------------------------

    def sell_for_funding(self, needed_micros: int) -> dict[str, Any] | None:
        """Sell from the largest position to raise ``needed_micros``. None if not possible.

        Used by auto top-up: the agent funds its own spending from its portfolio before
        tapping the Robinhood treasury. Not subject to the per-trade/daily trading caps
        (it's liquidity, not a trading decision) but respects the kill switch.
        """
        rules = self.rules()
        if not (rules.trading_enabled and rules.fund_from_portfolio):
            return None
        best = None
        for symbol, pos in self._store.positions().items():
            try:
                quote = self._prices.quote(symbol)
            except PriceError:
                continue
            value = pos["shares_units"] * quote.price_micros // SHARE
            if value >= needed_micros and (best is None or value > best[2]):
                best = (symbol, pos, value, quote)
        if best is None:
            return None
        symbol, pos, _, quote = best
        shares = min(pos["shares_units"], -(-needed_micros * SHARE // quote.price_micros))  # ceil
        fill = self._exchange.sell(symbol, shares, quote)
        cost_removed = pos["cost_micros"] * fill.shares_units // pos["shares_units"]
        self._store.apply_fill(symbol, -fill.shares_units, -cost_removed)
        txn = self._ledger.credit(
            fill.usd_micros,
            f"Sold d{symbol} to fund the wallet",
            rail=TRADE_RAIL,
            reference=fill.reference,
            simulated=fill.simulated,
            explorer_url=fill.explorer_url,
            metadata={"symbol": symbol, "shares": shares_str(fill.shares_units), "purpose": "funding"},
        )
        return {
            "symbol": symbol,
            "token": f"d{symbol}",
            "shares": shares_str(fill.shares_units),
            "amount_micros": fill.usd_micros,
            "price_usd": micros_to_usd(fill.price_micros),
            "txs": fill.txs,
            "simulated": fill.simulated,
            "transaction_id": txn.id,
        }
