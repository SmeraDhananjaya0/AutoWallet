"""Trading rules: the guardrails every proposal and every execution is checked against."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from backend.ledger import micros_to_usd, usd_to_micros
from backend.trading.prices import FEEDS


@dataclass
class TradingRules:
    trading_enabled: bool = True  # the kill switch
    max_trade_usd: float = 25.0
    daily_limit_usd: float = 100.0  # buys + sells, rolling 24h
    allowed_symbols: list[str] = field(default_factory=lambda: list(FEEDS))
    fund_from_portfolio: bool = True  # sell holdings before tapping the Robinhood treasury

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TradingRules":
        known = {f.name for f in fields(cls)}
        rules = cls(**{k: v for k, v in (data or {}).items() if k in known})
        rules.allowed_symbols = [s.upper() for s in rules.allowed_symbols if s.upper() in FEEDS]
        if rules.max_trade_usd <= 0 or rules.daily_limit_usd <= 0:
            raise ValueError("limits must be positive")
        return rules

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check(
    rules: TradingRules,
    *,
    symbol: str,
    usd_micros: int,
    executed_today_micros: int,
    cash_micros: int | None = None,
) -> list[dict[str, Any]]:
    """Return one entry per rule: {rule, ok, detail}. A proposal passes only if all are ok."""
    max_trade = usd_to_micros(rules.max_trade_usd)
    daily = usd_to_micros(rules.daily_limit_usd)
    after = executed_today_micros + usd_micros
    cash_check = []
    if cash_micros is not None:  # buys only
        cash_check = [
            {
                "rule": "Enough cash",
                "ok": usd_micros <= cash_micros,
                "detail": f"${micros_to_usd(cash_micros):,.2f} available",
            }
        ]
    return cash_check + [
        {
            "rule": "Trading enabled",
            "ok": rules.trading_enabled,
            "detail": "on" if rules.trading_enabled else "kill switch is on",
        },
        {
            "rule": "Allowed ticker",
            "ok": symbol in rules.allowed_symbols,
            "detail": symbol if symbol in rules.allowed_symbols else f"{symbol} is not on the allowed list",
        },
        {
            "rule": "Per-trade cap",
            "ok": usd_micros <= max_trade,
            "detail": f"${micros_to_usd(usd_micros):,.2f} of ${rules.max_trade_usd:,.2f} max",
        },
        {
            "rule": "Daily limit",
            "ok": after <= daily,
            "detail": f"${micros_to_usd(after):,.2f} of ${rules.daily_limit_usd:,.2f} in 24h",
        },
    ]


def passed(checks: list[dict[str, Any]]) -> bool:
    return all(c["ok"] for c in checks)
