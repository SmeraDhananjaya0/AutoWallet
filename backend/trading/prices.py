"""Real prices for the demo stock tokens, read from Chainlink on Robinhood Chain mainnet.

Chainlink runs a price feed for each Robinhood Stock Token on mainnet (chain 4663).
Reading one is a free ``eth_call``: no wallet, no gas, no money. The demo trades
play-money tokens on testnet at these real prices.

Feed list: https://reference-data-directory.vercel.app/feeds-robinhood-mainnet.json
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("autowallet.trading.prices")

MAINNET_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
CACHE_SECONDS = 30

# symbol -> (Chainlink AggregatorV3 proxy on Robinhood Chain mainnet, display name)
FEEDS: dict[str, tuple[str, str]] = {
    "NVDA": ("0x379EC4f7C378F34a1B47E4F3cbeBCbAC3E8E9F15", "NVIDIA"),
    "AAPL": ("0x6B22A786bAa607d76728168703a39Ea9C99f2cD0", "Apple"),
    "TSLA": ("0x4A1166a659A55625345e9515b32adECea5547C38", "Tesla"),
    "GOOGL": ("0xF6f373a037c30F0e5010d854385cA89185AE638b", "Alphabet"),
    "MSFT": ("0x45C3C877C15E6BA2EBB19eA114Ea508d14C1Af2E", "Microsoft"),
    "SPY": ("0x319724394D3A0e3669269846abE664Cd621f9f6A", "S&P 500 ETF"),
}

AGGREGATOR_ABI = [
    {
        "name": "latestRoundData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
    },
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
]


class PriceError(Exception):
    pass


def normalize_symbol(symbol: str) -> str:
    """'nvda', 'dNVDA', '$NVDA' -> 'NVDA' (demo tokens are the ticker with a 'd' prefix)."""
    s = symbol.strip().upper().lstrip("$")
    if s not in FEEDS and s.startswith("D") and s[1:] in FEEDS:
        s = s[1:]
    return s


@dataclass
class Quote:
    symbol: str
    name: str
    price_micros: int  # USD per share, 6 decimals
    updated_at: int  # unix seconds (feed update time)
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "price_usd": self.price_micros / 1_000_000,
            "updated_at": self.updated_at,
            "age_minutes": round((time.time() - self.updated_at) / 60, 1),
            "source": self.source,
        }


class ChainlinkPrices:
    def __init__(self, rpc_url: str = MAINNET_RPC_URL):
        self._rpc_url = rpc_url
        self._w3 = None
        self._cache: dict[str, tuple[float, Quote]] = {}
        self._lock = threading.Lock()

    def _web3(self) -> Any:
        if self._w3 is None:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url, request_kwargs={"timeout": 15}))
        return self._w3

    def symbols(self) -> list[str]:
        return list(FEEDS)

    def quote(self, symbol: str) -> Quote:
        symbol = normalize_symbol(symbol)
        if symbol not in FEEDS:
            raise PriceError(f"unknown symbol {symbol!r}; supported: {', '.join(FEEDS)}")
        with self._lock:
            cached = self._cache.get(symbol)
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                return cached[1]
        address, name = FEEDS[symbol]
        try:
            from web3 import Web3

            feed = self._web3().eth.contract(address=Web3.to_checksum_address(address), abi=AGGREGATOR_ABI)
            _, answer, _, updated_at, _ = feed.functions.latestRoundData().call()
            decimals = feed.functions.decimals().call()
        except Exception as exc:
            raise PriceError(f"Chainlink read failed for {symbol}: {exc}") from exc
        if answer <= 0:
            raise PriceError(f"Chainlink returned a non-positive price for {symbol}")
        quote = Quote(
            symbol=symbol,
            name=name,
            price_micros=int(answer) * 10**6 // 10**decimals,
            updated_at=int(updated_at),
            source="Chainlink on Robinhood Chain mainnet",
        )
        with self._lock:
            self._cache[symbol] = (time.monotonic(), quote)
        return quote


class FixedPrices:
    """Deterministic prices for tests / offline use."""

    def __init__(self, prices_usd: dict[str, float] | None = None):
        self._prices = prices_usd or {"NVDA": 200.0, "AAPL": 300.0, "TSLA": 350.0, "GOOGL": 300.0, "MSFT": 500.0, "SPY": 700.0}

    def symbols(self) -> list[str]:
        return list(self._prices)

    def quote(self, symbol: str) -> Quote:
        symbol = normalize_symbol(symbol)
        if symbol not in self._prices:
            raise PriceError(f"unknown symbol {symbol!r}")
        name = FEEDS.get(symbol, ("", symbol))[1]
        return Quote(symbol, name, int(self._prices[symbol] * 1_000_000), int(time.time()), "fixed test prices")
