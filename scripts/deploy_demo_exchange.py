"""Deploy the demo stock exchange to Robinhood Chain TESTNET (play money only).

Deploys DemoExchange plus one DemoStockToken per symbol (dNVDA, dAAPL, ...), lists
them, seeds the exchange with tUSDG so it can pay out sells, posts initial prices
from Chainlink on mainnet, and records the addresses in backend/.env:

    DEMO_EXCHANGE_ADDRESS=0x...
    DEMO_STOCK_TOKENS={"NVDA": "0x...", ...}

    python scripts/deploy_demo_exchange.py [--reserve 10000]

Needs tUSDG already deployed (scripts/deploy_test_token.py) with the treasury as its owner.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import solcx
from dotenv import dotenv_values
from eth_account import Account
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backend.trading.prices import FEEDS, ChainlinkPrices  # noqa: E402
from deploy_test_token import CHAIN_ID, EXPLORER, RPC_URL, SOLC_VERSION, send  # noqa: E402

ENV_PATH = ROOT / "backend" / ".env"
TUSDG_MINT_ABI = [
    {"name": "mint", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "outputs": []},
]


def compile_contracts() -> dict[str, dict]:
    solcx.install_solc(SOLC_VERSION)
    compiled = solcx.compile_files(
        [str(ROOT / "contracts" / "DemoStockToken.sol"), str(ROOT / "contracts" / "DemoExchange.sol")],
        output_values=["abi", "bin"],
        solc_version=SOLC_VERSION,
        evm_version="paris",
    )
    return {key.split(":")[-1]: value for key, value in compiled.items()}


def set_env(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    if re.search(rf"^{key}=", text, re.M):
        return re.sub(rf"^{key}=.*$", lambda _: line, text, flags=re.M)
    return text.rstrip("\n") + f"\n{line}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reserve", type=float, default=10_000, help="tUSDG to seed the exchange with")
    args = parser.parse_args()

    env = dotenv_values(ENV_PATH)
    treasury = Account.from_key(env["TREASURY_PRIVATE_KEY"])
    tusdg = Web3.to_checksum_address(env["ROBINHOOD_CHAIN_TOKEN_ADDRESS"])

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
    if w3.eth.chain_id != CHAIN_ID:
        raise SystemExit(f"unexpected chain id {w3.eth.chain_id}; this script is testnet-only")

    artifacts = compile_contracts()
    exchange_art, token_art = artifacts["DemoExchange"], artifacts["DemoStockToken"]
    tx = {"from": treasury.address}

    receipt = send(w3, treasury, w3.eth.contract(abi=exchange_art["abi"], bytecode=exchange_art["bin"])
                   .constructor(tusdg).build_transaction(tx))
    exchange = w3.eth.contract(address=receipt.contractAddress, abi=exchange_art["abi"])
    print(f"DemoExchange: {exchange.address}\n  {EXPLORER}/address/{exchange.address}")

    tokens: dict[str, str] = {}
    for symbol, (_, name) in FEEDS.items():
        receipt = send(w3, treasury, w3.eth.contract(abi=token_art["abi"], bytecode=token_art["bin"])
                       .constructor(f"AutoWallet Demo {name}", f"d{symbol}").build_transaction(tx))
        token = w3.eth.contract(address=receipt.contractAddress, abi=token_art["abi"])
        send(w3, treasury, token.functions.setMinter(exchange.address).build_transaction(tx))
        send(w3, treasury, exchange.functions.list(token.address).build_transaction(tx))
        tokens[symbol] = token.address
        print(f"  d{symbol}: {token.address}")

    # Seed the exchange's tUSDG reserve so it can pay out sells.
    usdg = w3.eth.contract(address=tusdg, abi=TUSDG_MINT_ABI)
    send(w3, treasury, usdg.functions.mint(exchange.address, int(args.reserve * 10**6)).build_transaction(tx))
    print(f"  seeded exchange with {args.reserve:g} tUSDG")

    # Initial prices from Chainlink on mainnet.
    prices = ChainlinkPrices()
    quotes = {s: prices.quote(s) for s in tokens}
    send(w3, treasury, exchange.functions.setPrices(
        [Web3.to_checksum_address(tokens[s]) for s in tokens],
        [quotes[s].price_micros for s in tokens],
    ).build_transaction(tx))
    print("  prices: " + ", ".join(f"{s} ${q.price_micros / 1e6:,.2f}" for s, q in quotes.items()))

    text = ENV_PATH.read_text()
    text = set_env(text, "DEMO_EXCHANGE_ADDRESS", exchange.address)
    text = set_env(text, "DEMO_STOCK_TOKENS", json.dumps(tokens, separators=(",", ":")))
    ENV_PATH.write_text(text)
    print("Saved DEMO_EXCHANGE_ADDRESS and DEMO_STOCK_TOKENS to backend/.env")


if __name__ == "__main__":
    main()
