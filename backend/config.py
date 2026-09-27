"""Environment-driven settings.

This is the only module that loads ``backend/.env``. Everything else reads a
``Settings`` instance, which keeps configuration in one place and makes tests
able to build isolated settings with ``dataclasses.replace``.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR / ".env")

# Robinhood Chain network presets (docs.robinhood.com/chain/connecting).
ROBINHOOD_CHAIN_NETWORKS = {
    "mainnet": {
        "chain_id": 4663,
        "rpc_url": "https://rpc.mainnet.chain.robinhood.com",
        "explorer_url": "https://robinhoodchain.blockscout.com",
        # USDG, 6 decimals (docs.robinhood.com/chain/contracts).
        "token_address": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
    },
    "testnet": {
        "chain_id": 46630,
        "rpc_url": "https://rpc.testnet.chain.robinhood.com",
        "explorer_url": "https://explorer.testnet.chain.robinhood.com",
        # No canonical testnet stablecoin is published; set ROBINHOOD_CHAIN_TOKEN_ADDRESS.
        "token_address": "",
    },
}

CHAIN_MODES = ("simulated", "testnet", "mainnet")
CRYPTO_MODES = ("simulated", "live")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value else default


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _env(name, default).lower()
    if value not in allowed:
        raise ValueError(f"{name}={value!r} must be one of {allowed}")
    return value


@dataclass(frozen=True)
class Settings:
    # Claude
    anthropic_api_key: str
    claude_model: str
    claude_fallbacks: bool

    # Wallet / ledger
    initial_balance_usd: float
    ledger_db_path: str
    card_topup_usd: float
    demo_mode: bool

    # Stripe (card rail + card top-ups)
    stripe_secret_key: str
    stripe_min_charge_usd: float
    stripe_allow_live_keys: bool

    # Which rail settles sub-$0.50 payments
    micropayment_rail: str

    # Robinhood Chain (USDG micropayments)
    chain_mode: str
    chain_id: int
    chain_rpc_url: str
    chain_explorer_url: str
    chain_token_address: str
    chain_token_decimals: int
    agent_wallet_private_key: str
    treasury_private_key: str
    merchant_address: str

    # Robinhood Crypto Trading API (treasury buys)
    crypto_mode: str
    crypto_api_key: str
    crypto_private_key_b64: str
    crypto_base_url: str
    crypto_topup_symbol: str

    # Auto top-up policy
    auto_topup_enabled: bool
    auto_topup_threshold_usd: float
    auto_topup_amount_usd: float
    auto_topup_daily_cap_usd: float

    # Circle (fallback micropayment rail)
    circle_api_key: str
    circle_entity_secret: str
    circle_wallet_address: str
    circle_source_wallet_id: str
    circle_token_id: str
    circle_api_base_url: str

    # Scoped payment tokens + public endpoints
    spt_signing_secret: str
    spt_issuer_key: str
    spt_max_amount_usd: float
    public_base_url: str

    # Search provider
    brave_search_api_key: str

    @classmethod
    def from_env(cls) -> "Settings":
        chain_mode = _choice("ROBINHOOD_CHAIN_MODE", "simulated", CHAIN_MODES)
        preset = ROBINHOOD_CHAIN_NETWORKS.get(chain_mode, ROBINHOOD_CHAIN_NETWORKS["mainnet"])

        return cls(
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            claude_model=_env("CLAUDE_MODEL", "claude-opus-5"),
            claude_fallbacks=_bool("CLAUDE_FALLBACKS", True),
            initial_balance_usd=_float("WALLET_INITIAL_BALANCE_USD", 10.0),
            # Empty string = in-memory ledger (resets on restart).
            ledger_db_path=_env("LEDGER_DB_PATH", str(BACKEND_DIR / "data" / "autowallet.db")),
            card_topup_usd=_float("CARD_TOPUP_USD", 10.0),
            demo_mode=_bool("DEMO_MODE", True),
            stripe_secret_key=_env("STRIPE_SECRET_KEY"),
            stripe_min_charge_usd=_float("STRIPE_MIN_CHARGE_USD", 0.50),
            stripe_allow_live_keys=_bool("STRIPE_ALLOW_LIVE_KEYS", False),
            micropayment_rail=_choice(
                "MICROPAYMENT_RAIL", "robinhood_chain", ("robinhood_chain", "circle")
            ),
            chain_mode=chain_mode,
            chain_id=int(_env("ROBINHOOD_CHAIN_ID") or preset["chain_id"]),
            chain_rpc_url=_env("ROBINHOOD_CHAIN_RPC_URL") or preset["rpc_url"],
            chain_explorer_url=_env("ROBINHOOD_CHAIN_EXPLORER_URL") or preset["explorer_url"],
            chain_token_address=_env("ROBINHOOD_CHAIN_TOKEN_ADDRESS") or preset["token_address"],
            chain_token_decimals=int(_env("ROBINHOOD_CHAIN_TOKEN_DECIMALS") or 6),
            agent_wallet_private_key=_env("AGENT_WALLET_PRIVATE_KEY"),
            treasury_private_key=_env("TREASURY_PRIVATE_KEY"),
            merchant_address=_env("MERCHANT_ADDRESS"),
            crypto_mode=_choice("ROBINHOOD_CRYPTO_MODE", "simulated", CRYPTO_MODES),
            crypto_api_key=_env("ROBINHOOD_API_KEY"),
            crypto_private_key_b64=_env("ROBINHOOD_PRIVATE_KEY"),
            crypto_base_url=_env("ROBINHOOD_BASE_URL", "https://trading.robinhood.com").rstrip("/"),
            crypto_topup_symbol=_env("ROBINHOOD_TOPUP_SYMBOL", "USDC-USD"),
            auto_topup_enabled=_bool("AUTO_TOPUP_ENABLED", True),
            auto_topup_threshold_usd=_float("AUTO_TOPUP_THRESHOLD_USD", 1.0),
            auto_topup_amount_usd=_float("AUTO_TOPUP_AMOUNT_USD", 5.0),
            auto_topup_daily_cap_usd=_float("AUTO_TOPUP_DAILY_CAP_USD", 25.0),
            circle_api_key=_env("CIRCLE_API_KEY"),
            circle_entity_secret=_env("CIRCLE_ENTITY_SECRET"),
            circle_wallet_address=_env("CIRCLE_WALLET_ADDRESS"),
            circle_source_wallet_id=_env("CIRCLE_SOURCE_WALLET_ID"),
            circle_token_id=_env("CIRCLE_TOKEN_ID"),
            circle_api_base_url=_env("CIRCLE_API_BASE_URL"),
            # A random per-process secret is fine for a demo: tokens die with the server.
            spt_signing_secret=_env("SPT_SIGNING_SECRET") or secrets.token_hex(32),
            spt_issuer_key=_env("SPT_ISSUER_KEY"),
            spt_max_amount_usd=_float("SPT_MAX_AMOUNT_USD", 1.0),
            public_base_url=_env("PUBLIC_ENDPOINT_URL", "http://localhost:8000").rstrip("/"),
            brave_search_api_key=_env("BRAVE_SEARCH_API_KEY"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
