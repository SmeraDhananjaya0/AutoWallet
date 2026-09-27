"""Robinhood Chain rail: USDG (ERC-20) transfers on Robinhood's Arbitrum Orbit L2.

Modes (ROBINHOOD_CHAIN_MODE):
  simulated - no network calls; returns clearly-marked fake tx hashes (default)
  testnet   - chain 46630; set ROBINHOOD_CHAIN_TOKEN_ADDRESS to a test token
  mainnet   - chain 4663, real USDG, real money
"""

from __future__ import annotations

import logging
import secrets
import threading
from typing import Any

from backend.config import Settings
from backend.ledger import micros_to_usd
from backend.rails.base import FAILED, SETTLED, PaymentResult

logger = logging.getLogger("autowallet.rails.robinhood_chain")

ERC20_ABI = [
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "Transfer",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
    },
]

RECEIPT_TIMEOUT_SECONDS = 30
SIMULATED_AGENT_ADDRESS = "0x000000000000000000000000000000000000a6e7"
SIMULATED_MERCHANT_ADDRESS = "0x000000000000000000000000000000000000beef"


class RobinhoodChainRail:
    name = "robinhood_chain"

    def __init__(self, settings: Settings):
        self._s = settings
        self.mode = settings.chain_mode
        self.simulated = self.mode == "simulated"
        self._nonce_lock = threading.Lock()
        self._sim_transfers: dict[str, dict[str, Any]] = {}
        self._config_error: str | None = None
        self._w3 = None
        self._token = None
        self._agent = None
        self._treasury = None
        self.merchant_address = settings.merchant_address or SIMULATED_MERCHANT_ADDRESS

        if not self.simulated:
            self._init_live()

    def _init_live(self) -> None:
        from eth_account import Account
        from web3 import Web3

        missing = [
            name
            for name, value in (
                ("ROBINHOOD_CHAIN_TOKEN_ADDRESS", self._s.chain_token_address),
                ("AGENT_WALLET_PRIVATE_KEY", self._s.agent_wallet_private_key),
                ("MERCHANT_ADDRESS", self._s.merchant_address),
            )
            if not value
        ]
        if missing:
            self._config_error = f"missing {', '.join(missing)}"
            return

        self._w3 = Web3(Web3.HTTPProvider(self._s.chain_rpc_url, request_kwargs={"timeout": 20}))
        self._token = self._w3.eth.contract(
            address=Web3.to_checksum_address(self._s.chain_token_address), abi=ERC20_ABI
        )
        self._agent = Account.from_key(self._s.agent_wallet_private_key)
        if self._s.treasury_private_key:
            self._treasury = Account.from_key(self._s.treasury_private_key)
        self.merchant_address = Web3.to_checksum_address(self._s.merchant_address)

    # --- public API -----------------------------------------------------

    @property
    def agent_address(self) -> str:
        return self._agent.address if self._agent else SIMULATED_AGENT_ADDRESS

    @property
    def token_address(self) -> str:
        return self._s.chain_token_address

    def available(self) -> tuple[bool, str]:
        if self.simulated:
            return True, "simulated"
        if self._config_error:
            return False, self._config_error
        return True, self.mode

    def to_units(self, amount_micros: int) -> int:
        """Ledger micros (6 dp) -> token base units."""
        decimals = self._s.chain_token_decimals
        if decimals >= 6:
            return amount_micros * 10 ** (decimals - 6)
        return amount_micros // 10 ** (6 - decimals)

    def pay(self, amount_micros: int, memo: str) -> PaymentResult:
        """Agent wallet -> merchant."""
        return self._transfer("agent", self.merchant_address, amount_micros, memo)

    def fund_agent_from_treasury(self, amount_micros: int, memo: str) -> PaymentResult:
        """Treasury wallet -> agent wallet (used by auto top-up)."""
        if not self.simulated and self._treasury is None:
            return PaymentResult(self.name, amount_micros, FAILED, error="TREASURY_PRIVATE_KEY not set")
        return self._transfer("treasury", self.agent_address, amount_micros, memo)

    def simulate_external_payment(self, amount_micros: int) -> PaymentResult:
        """Simulated mode only: pretend a third-party agent paid the merchant (for x402 demos/tests)."""
        if not self.simulated:
            raise RuntimeError("only available in simulated mode")
        return self._transfer("external", self.merchant_address, amount_micros, "external x402 payment")

    def token_balance_micros(self, who: str = "agent") -> int | None:
        if self.simulated or self._token is None:
            return None
        account = self._agent if who == "agent" else self._treasury
        if account is None:
            return None
        units = self._token.functions.balanceOf(account.address).call()
        decimals = self._s.chain_token_decimals
        return units * 10**6 // 10**decimals

    def verify_payment(self, tx_hash: str, min_amount_micros: int) -> tuple[bool, str]:
        """Check that ``tx_hash`` moved at least ``min_amount_micros`` of the token to the merchant."""
        if self.simulated:
            transfer = self._sim_transfers.get(tx_hash.lower())
            if not transfer:
                return False, "unknown simulated transaction"
            if transfer["to"].lower() != self.merchant_address.lower():
                return False, "transfer was not to the merchant"
            if transfer["amount_micros"] < min_amount_micros:
                return False, "transfer amount too small"
            return True, "ok"

        if self._w3 is None:
            return False, self._config_error or "chain not configured"
        try:
            receipt = self._w3.eth.get_transaction_receipt(tx_hash)
        except Exception as exc:  # web3 raises TransactionNotFound and transport errors
            return False, f"receipt lookup failed: {exc}"
        if receipt.status != 1:
            return False, "transaction reverted"
        needed = self.to_units(min_amount_micros)
        for event in self._token.events.Transfer().process_receipt(receipt):
            if (
                event["address"].lower() == self._token.address.lower()
                and event["args"]["to"].lower() == self.merchant_address.lower()
                and event["args"]["value"] >= needed
            ):
                return True, "ok"
        return False, "no qualifying token transfer to the merchant in this transaction"

    def explorer_tx_url(self, tx_hash: str) -> str | None:
        if self.simulated:
            return None
        return f"{self._s.chain_explorer_url}/tx/{tx_hash}"

    def status(self) -> dict[str, Any]:
        ok, reason = self.available()
        data: dict[str, Any] = {
            "rail": self.name,
            "available": ok,
            "detail": reason,
            "mode": self.mode,
            "chain_id": self._s.chain_id,
            "token_address": self._s.chain_token_address,
            "agent_address": self.agent_address,
            "merchant_address": self.merchant_address,
            "treasury_configured": self.simulated or self._treasury is not None,
        }
        if not self.simulated and ok:
            try:
                bal = self.token_balance_micros("agent")
                data["agent_token_balance_usd"] = micros_to_usd(bal) if bal is not None else None
            except Exception as exc:
                data["agent_token_balance_error"] = str(exc)
        return data

    # --- internals ------------------------------------------------------

    def _transfer(self, source: str, to: str, amount_micros: int, memo: str) -> PaymentResult:
        if self.simulated:
            tx_hash = "0x" + secrets.token_hex(32)
            self._sim_transfers[tx_hash] = {"to": to, "amount_micros": amount_micros, "memo": memo}
            return PaymentResult(
                rail=self.name,
                amount_micros=amount_micros,
                status=SETTLED,
                reference=tx_hash,
                simulated=True,
            )

        ok, reason = self.available()
        if not ok:
            return PaymentResult(self.name, amount_micros, FAILED, error=reason)

        account = self._agent if source == "agent" else self._treasury
        try:
            with self._nonce_lock:
                nonce = self._w3.eth.get_transaction_count(account.address, "pending")
                tx = self._token.functions.transfer(to, self.to_units(amount_micros)).build_transaction(
                    {"from": account.address, "nonce": nonce, "chainId": self._s.chain_id}
                )
                signed = account.sign_transaction(tx)
                tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("Robinhood Chain transfer failed (%s -> %s): %s", source, to, exc)
            return PaymentResult(self.name, amount_micros, FAILED, error=str(exc))

        hex_hash = "0x" + bytes(tx_hash).hex()
        if receipt.status != 1:
            return PaymentResult(
                self.name, amount_micros, FAILED, reference=hex_hash, error="transaction reverted"
            )
        logger.info("Robinhood Chain transfer %s -> %s settled: %s", source, to, hex_hash)
        return PaymentResult(
            rail=self.name,
            amount_micros=amount_micros,
            status=SETTLED,
            reference=hex_hash,
            explorer_url=self.explorer_tx_url(hex_hash),
        )
