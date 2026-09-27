"""Robinhood Crypto Trading API client (docs.robinhood.com/crypto/trading).

Every request is signed with Ed25519. The signed message is the concatenation
``api_key + timestamp + path + method + body`` where ``path`` includes the query
string, ``timestamp`` is Unix seconds (valid for 30s), and ``body`` is the exact
JSON string sent (omitted when there is no body).

The API has **no withdrawal endpoint** - it covers accounts, holdings, market
data and orders. Moving purchased crypto to an on-chain wallet is therefore not
automatable through it; see ``auto_topup.py`` for how the demo handles that.

``SimulatedRobinhoodCrypto`` mirrors the interface without network calls.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from nacl.signing import SigningKey

logger = logging.getLogger("autowallet.treasury.robinhood")

API_PREFIX = "/api/v1/crypto"
ORDER_POLL_SECONDS = 15
TERMINAL_ORDER_STATES = {"filled", "canceled", "cancelled", "failed", "rejected"}


class RobinhoodCryptoError(Exception):
    pass


class CryptoTreasury(Protocol):
    simulated: bool

    def buy_usd_amount(self, symbol: str, usd: Decimal) -> dict[str, Any]:
        """Buy roughly ``usd`` worth of ``symbol``. Returns the final order dict."""

    def status(self) -> dict[str, Any]: ...


def sign_request(
    signing_key: SigningKey, api_key: str, timestamp: int, path: str, method: str, body: str
) -> str:
    message = f"{api_key}{timestamp}{path}{method.upper()}{body}"
    return base64.b64encode(signing_key.sign(message.encode("utf-8")).signature).decode()


class RobinhoodCryptoClient:
    simulated = False

    def __init__(self, api_key: str, private_key_b64: str, base_url: str = "https://trading.robinhood.com"):
        if not api_key or not private_key_b64:
            raise RobinhoodCryptoError("ROBINHOOD_API_KEY and ROBINHOOD_PRIVATE_KEY are required in live mode")
        seed = base64.b64decode(private_key_b64)
        if len(seed) != 32:
            raise RobinhoodCryptoError("ROBINHOOD_PRIVATE_KEY must be a base64-encoded 32-byte Ed25519 seed")
        self._api_key = api_key
        self._signing_key = SigningKey(seed)
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=15.0)

    # --- transport ------------------------------------------------------

    def request(
        self, method: str, path: str, *, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if query:
            path = f"{path}?{urlencode(query, doseq=True)}"
        body_str = json.dumps(body) if body is not None else ""
        timestamp = int(time.time())
        headers = {
            "x-api-key": self._api_key,
            "x-timestamp": str(timestamp),
            "x-signature": sign_request(self._signing_key, self._api_key, timestamp, path, method, body_str),
            "Content-Type": "application/json; charset=utf-8",
        }
        resp = self._http.request(
            method.upper(), f"{self._base_url}{path}", headers=headers, content=body_str or None
        )
        if resp.status_code >= 400:
            raise RobinhoodCryptoError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    # --- endpoints ------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        return self.request("GET", f"{API_PREFIX}/trading/accounts/")

    def get_holdings(self, *asset_codes: str) -> dict[str, Any]:
        query = {"asset_code": list(asset_codes)} if asset_codes else None
        return self.request("GET", f"{API_PREFIX}/trading/holdings/", query=query)

    def get_trading_pair(self, symbol: str) -> dict[str, Any]:
        data = self.request("GET", f"{API_PREFIX}/trading/trading_pairs/", query={"symbol": symbol})
        results = data.get("results") or []
        if not results:
            raise RobinhoodCryptoError(f"{symbol} is not a tradable pair on this account")
        return results[0]

    def get_best_ask(self, symbol: str) -> Decimal:
        data = self.request("GET", f"{API_PREFIX}/marketdata/best_bid_ask/", query={"symbol": symbol})
        results = data.get("results") or []
        if not results:
            raise RobinhoodCryptoError(f"no quote for {symbol}")
        quote = results[0]
        price = quote.get("ask_inclusive_of_buy_spread") or quote.get("price")
        if price is None:
            raise RobinhoodCryptoError(f"unexpected quote shape for {symbol}")
        return Decimal(str(price))

    def place_market_order(self, symbol: str, side: str, asset_quantity: Decimal) -> dict[str, Any]:
        body = {
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "type": "market",
            "symbol": symbol,
            "market_order_config": {"asset_quantity": format(asset_quantity, "f")},
        }
        return self.request("POST", f"{API_PREFIX}/trading/orders/", body=body)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self.request("GET", f"{API_PREFIX}/trading/orders/{order_id}/")

    # --- high level -----------------------------------------------------

    def buy_usd_amount(self, symbol: str, usd: Decimal) -> dict[str, Any]:
        pair = self.get_trading_pair(symbol)
        increment = Decimal(str(pair.get("asset_increment") or "0.01"))
        ask = self.get_best_ask(symbol)
        quantity = (usd / ask).quantize(increment, rounding=ROUND_DOWN)
        min_size = Decimal(str(pair.get("min_order_size") or "0"))
        if quantity <= 0 or quantity < min_size:
            raise RobinhoodCryptoError(f"${usd} buys {quantity} {symbol}, below the minimum order size {min_size}")

        order = self.place_market_order(symbol, "buy", quantity)
        order_id = order.get("id")
        logger.info("Robinhood market buy placed: %s %s (order %s)", quantity, symbol, order_id)

        deadline = time.monotonic() + ORDER_POLL_SECONDS
        while order_id and str(order.get("state", "")).lower() not in TERMINAL_ORDER_STATES:
            if time.monotonic() > deadline:
                break
            time.sleep(1)
            order = self.get_order(order_id)
        return order

    def status(self) -> dict[str, Any]:
        try:
            account = self.get_account()
            return {"mode": "live", "connected": True, "account_status": account.get("status")}
        except Exception as exc:
            return {"mode": "live", "connected": False, "error": str(exc)}


class SimulatedRobinhoodCrypto:
    simulated = True

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.orders: list[dict[str, Any]] = []

    def buy_usd_amount(self, symbol: str, usd: Decimal) -> dict[str, Any]:
        if self.fail:
            raise RobinhoodCryptoError("simulated Robinhood failure")
        order = {
            "id": f"sim-{uuid.uuid4()}",
            "symbol": symbol,
            "side": "buy",
            "type": "market",
            "state": "filled",
            "filled_asset_quantity": format(usd.quantize(Decimal("0.01")), "f"),
            "average_price": "1.00",
        }
        self.orders.append(order)
        return order

    def status(self) -> dict[str, Any]:
        return {"mode": "simulated", "connected": True, "orders_placed": len(self.orders)}
