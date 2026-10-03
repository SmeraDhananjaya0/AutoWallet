"""Client for the DemoExchange contract on Robinhood Chain testnet (play money only).

testnet mode: real transactions. Before a trade it (1) re-posts the Chainlink
price from the treasury if the exchange's copy is stale or off, (2) approves
tUSDG once, then (3) buys or sells. Every step is a clickable explorer link.

simulated mode (ROBINHOOD_CHAIN_MODE=simulated, or no exchange deployed): same
math, no network, clearly-marked fake tx hashes.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from backend.rails.robinhood_chain import ChainTxError, RobinhoodChainRail
from backend.trading.prices import Quote

logger = logging.getLogger("autowallet.trading.exchange")

SHARE = 10**18
PRICE_REFRESH_SECONDS = 30 * 60  # re-post even an unchanged price well before the contract's 1h limit
PRICE_TOLERANCE = 0.0025  # re-post if the on-chain price is >0.25% off Chainlink
SLIPPAGE = 0.005  # min-out protection on the swap itself
MAX_UINT = 2**256 - 1

EXCHANGE_ABI = [
    {"name": "price", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "priceUpdatedAt", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "setPrices", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "tokens", "type": "address[]"}, {"name": "prices", "type": "uint256[]"}], "outputs": []},
    {"name": "buy", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "token", "type": "address"}, {"name": "usdgIn", "type": "uint256"}, {"name": "minSharesOut", "type": "uint256"}],
     "outputs": [{"name": "shares", "type": "uint256"}]},
    {"name": "sell", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "token", "type": "address"}, {"name": "sharesIn", "type": "uint256"}, {"name": "minUsdgOut", "type": "uint256"}],
     "outputs": [{"name": "usdgOut", "type": "uint256"}]},
    {"name": "Trade", "type": "event", "anonymous": False, "inputs": [
        {"indexed": True, "name": "trader", "type": "address"},
        {"indexed": True, "name": "token", "type": "address"},
        {"indexed": False, "name": "isBuy", "type": "bool"},
        {"indexed": False, "name": "usdgAmount", "type": "uint256"},
        {"indexed": False, "name": "shares", "type": "uint256"},
        {"indexed": False, "name": "price", "type": "uint256"}]},
]
ERC20_ABI = [
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
]


class ExchangeError(Exception):
    pass


@dataclass
class Fill:
    side: str
    symbol: str
    shares_units: int  # 1e18 = 1 share
    usd_micros: int
    price_micros: int
    simulated: bool
    txs: list[dict[str, Any]] = field(default_factory=list)  # [{label, hash, explorer_url}]

    @property
    def reference(self) -> str:
        return self.txs[-1]["hash"] if self.txs else ""

    @property
    def explorer_url(self) -> str | None:
        return self.txs[-1].get("explorer_url") if self.txs else None


class DemoExchange:
    def __init__(self, chain: RobinhoodChainRail, exchange_address: str, token_addresses: dict[str, str]):
        self._chain = chain
        self.address = exchange_address
        self.tokens = {s.upper(): a for s, a in token_addresses.items()}
        self.simulated = chain.simulated or not exchange_address or not token_addresses
        self._exchange = None
        if not self.simulated and chain.web3 is not None:
            from web3 import Web3

            w3 = chain.web3
            self._exchange = w3.eth.contract(address=Web3.to_checksum_address(exchange_address), abi=EXCHANGE_ABI)
            self._usdg = w3.eth.contract(address=Web3.to_checksum_address(chain.token_address), abi=ERC20_ABI)
            self.tokens = {s: Web3.to_checksum_address(a) for s, a in self.tokens.items()}
        elif not self.simulated:
            self.simulated = True  # chain not usable -> fall back to simulation

    def status(self) -> dict[str, Any]:
        return {
            "mode": "simulated" if self.simulated else self._chain.mode,
            "exchange_address": self.address or None,
            "tokens": {f"d{s}": a for s, a in self.tokens.items()},
        }

    def token_explorer_url(self, symbol: str) -> str | None:
        addr = self.tokens.get(symbol)
        if self.simulated or not addr:
            return None
        return self._chain.explorer_address_url(addr)

    # --- trading --------------------------------------------------------

    def buy(self, symbol: str, usd_micros: int, quote: Quote) -> Fill:
        shares = usd_micros * SHARE // quote.price_micros
        if shares <= 0:
            raise ExchangeError("amount too small for one share unit")
        if self.simulated:
            return Fill("buy", symbol, shares, usd_micros, quote.price_micros, True, [self._sim_tx("buy")])

        token = self._token(symbol)
        txs = self._ensure_price(symbol, token, quote)
        if self._usdg.functions.allowance(self._chain.address_of("agent"), self._exchange.address).call() < usd_micros:
            txs.append(self._send("agent", "approve tUSDG", self._usdg.functions.approve(self._exchange.address, MAX_UINT)))
        min_out = int(shares * (1 - SLIPPAGE))
        tx, receipt = self._send_with_receipt("agent", f"buy d{symbol}", self._exchange.functions.buy(token, usd_micros, min_out))
        txs.append(tx)
        event = self._trade_event(receipt)
        return Fill("buy", symbol, event["shares"], event["usdgAmount"], event["price"], False, txs)

    def sell(self, symbol: str, shares_units: int, quote: Quote) -> Fill:
        usd = shares_units * quote.price_micros // SHARE
        if usd <= 0:
            raise ExchangeError("position too small to sell")
        if self.simulated:
            return Fill("sell", symbol, shares_units, usd, quote.price_micros, True, [self._sim_tx("sell")])

        token = self._token(symbol)
        txs = self._ensure_price(symbol, token, quote)
        min_out = int(usd * (1 - SLIPPAGE))
        tx, receipt = self._send_with_receipt("agent", f"sell d{symbol}", self._exchange.functions.sell(token, shares_units, min_out))
        txs.append(tx)
        event = self._trade_event(receipt)
        return Fill("sell", symbol, event["shares"], event["usdgAmount"], event["price"], False, txs)

    # --- internals ------------------------------------------------------

    def _token(self, symbol: str) -> str:
        try:
            return self.tokens[symbol]
        except KeyError as exc:
            raise ExchangeError(f"d{symbol} is not listed on the demo exchange") from exc

    def _ensure_price(self, symbol: str, token: str, quote: Quote) -> list[dict[str, Any]]:
        """Post the Chainlink price to the exchange if its copy is stale or drifted."""
        onchain = self._exchange.functions.price(token).call()
        updated = self._exchange.functions.priceUpdatedAt(token).call()
        drift = abs(onchain - quote.price_micros) / quote.price_micros if onchain else 1.0
        if drift <= PRICE_TOLERANCE and time.time() - updated < PRICE_REFRESH_SECONDS:
            return []
        return [self._send("treasury", f"post {symbol} price", self._exchange.functions.setPrices([token], [quote.price_micros]))]

    def _send(self, who: str, label: str, call: Any) -> dict[str, Any]:
        return self._send_with_receipt(who, label, call)[0]

    def _send_with_receipt(self, who: str, label: str, call: Any) -> tuple[dict[str, Any], Any]:
        try:
            tx_hash, receipt = self._chain.send_call(who, call)
        except ChainTxError as exc:
            raise ExchangeError(f"{label} failed: {exc}") from exc
        logger.info("demo exchange: %s -> %s", label, tx_hash)
        return {"label": label, "hash": tx_hash, "explorer_url": self._chain.explorer_tx_url(tx_hash)}, receipt

    def _trade_event(self, receipt: Any) -> dict[str, int]:
        from web3.logs import DISCARD

        # The receipt also holds token Transfer logs; skip them quietly.
        events = self._exchange.events.Trade().process_receipt(receipt, errors=DISCARD)
        if not events:
            raise ExchangeError("trade succeeded but no Trade event was found")
        args = events[0]["args"]
        return {"shares": int(args["shares"]), "usdgAmount": int(args["usdgAmount"]), "price": int(args["price"])}

    @staticmethod
    def _sim_tx(label: str) -> dict[str, Any]:
        return {"label": label, "hash": "0x" + secrets.token_hex(32), "explorer_url": None}
