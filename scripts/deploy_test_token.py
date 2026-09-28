"""Deploy tUSDG (play-money USDG) to Robinhood Chain testnet and fund the demo wallets.

Deploys from the treasury wallet, mints tUSDG to the treasury and agent, and
records ROBINHOOD_CHAIN_TOKEN_ADDRESS in backend/.env. Testnet only.

    pip install py-solc-x
    python scripts/deploy_test_token.py [--treasury 1000] [--agent 100]

Needs AGENT_WALLET_PRIVATE_KEY and TREASURY_PRIVATE_KEY in backend/.env, and a
little testnet ETH in the treasury (faucet: https://faucet.quicknode.com/robinhood/testnet).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import solcx
from dotenv import dotenv_values
from eth_account import Account
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / "backend" / ".env"
CONTRACT = ROOT / "contracts" / "TestUSDG.sol"
RPC_URL = "https://rpc.testnet.chain.robinhood.com"
CHAIN_ID = 46630
EXPLORER = "https://explorer.testnet.chain.robinhood.com"
SOLC_VERSION = "0.8.24"


def send(w3: Web3, account, tx: dict) -> dict:
    tx = {**tx, "from": account.address}
    tx.setdefault("nonce", w3.eth.get_transaction_count(account.address, "pending"))
    tx["chainId"] = CHAIN_ID
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)  # Arbitrum gas includes L1 data cost
    tx["maxFeePerGas"] = w3.eth.gas_price * 2
    tx["maxPriorityFeePerGas"] = 0
    tx.pop("from")
    tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise SystemExit(f"transaction reverted: {EXPLORER}/tx/0x{bytes(tx_hash).hex()}")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--treasury", type=float, default=1000, help="tUSDG to mint to the treasury")
    parser.add_argument("--agent", type=float, default=100, help="tUSDG to mint to the agent")
    args = parser.parse_args()

    env = dotenv_values(ENV_PATH)
    treasury = Account.from_key(env["TREASURY_PRIVATE_KEY"])
    agent = Account.from_key(env["AGENT_WALLET_PRIVATE_KEY"])

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
    if w3.eth.chain_id != CHAIN_ID:
        raise SystemExit(f"unexpected chain id {w3.eth.chain_id}; this script is testnet-only")

    solcx.install_solc(SOLC_VERSION)
    compiled = solcx.compile_files(
        [str(CONTRACT)], output_values=["abi", "bin"], solc_version=SOLC_VERSION, evm_version="paris"
    )
    artifact = next(v for k, v in compiled.items() if k.endswith(":TestUSDG"))
    contract = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bin"])

    receipt = send(w3, treasury, contract.constructor().build_transaction({"from": treasury.address}))
    address = receipt.contractAddress
    print(f"tUSDG deployed: {address}\n  {EXPLORER}/address/{address}")

    token = w3.eth.contract(address=address, abi=artifact["abi"])
    for label, who, amount in (("treasury", treasury.address, args.treasury), ("agent", agent.address, args.agent)):
        units = int(amount * 10**6)
        send(w3, treasury, token.functions.mint(who, units).build_transaction({"from": treasury.address}))
        balance = token.functions.balanceOf(who).call() / 10**6
        print(f"  minted {amount:g} tUSDG to {label} {who} (balance {balance:g})")

    text = ENV_PATH.read_text()
    line = f"ROBINHOOD_CHAIN_TOKEN_ADDRESS={address}"
    if re.search(r"^ROBINHOOD_CHAIN_TOKEN_ADDRESS=", text, re.M):
        text = re.sub(r"^ROBINHOOD_CHAIN_TOKEN_ADDRESS=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    ENV_PATH.write_text(text)
    print(f"Saved {line} to backend/.env")


if __name__ == "__main__":
    main()
