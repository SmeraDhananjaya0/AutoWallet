"""Circle Programmable Wallets (W3S) rail: USDC transfers. Fallback micropayment rail."""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.padding import MGF1, OAEP
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from backend.config import Settings
from backend.rails.base import FAILED, SUBMITTED, PaymentResult

logger = logging.getLogger("autowallet.rails.circle")

_FAILED_STATES = {"failed", "denied", "cancelled", "canceled"}
_LOG_BODY_LIMIT = 300


class CircleRail:
    name = "circle"

    def __init__(self, settings: Settings):
        self._s = settings
        self._key_verified: bool | None = None

    @property
    def _base(self) -> str:
        if self._s.circle_api_base_url:
            return self._s.circle_api_base_url.rstrip("/")
        if self._s.circle_api_key.startswith(("TEST_API_KEY", "LIVE_API_KEY")):
            return "https://api.circle.com"
        return "https://api-sandbox.circle.com"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._s.circle_api_key}", "Content-Type": "application/json"}

    def _missing_config(self) -> list[str]:
        return [
            name
            for name, value in (
                ("CIRCLE_API_KEY", self._s.circle_api_key),
                ("CIRCLE_ENTITY_SECRET", self._s.circle_entity_secret),
                ("CIRCLE_WALLET_ADDRESS", self._s.circle_wallet_address),
                ("CIRCLE_SOURCE_WALLET_ID", self._s.circle_source_wallet_id),
                ("CIRCLE_TOKEN_ID", self._s.circle_token_id),
            )
            if not value
        ]

    def available(self) -> tuple[bool, str]:
        missing = self._missing_config()
        if missing:
            return False, f"missing {', '.join(missing)}"
        if self._key_verified is False:
            return False, "Circle API key verification failed"
        return True, "configured"

    def _verify_key_once(self) -> bool:
        if self._key_verified is None:
            try:
                resp = httpx.get(f"{self._base}/v1/w3s/config/entity", headers=self._headers(), timeout=15.0)
                self._key_verified = resp.status_code == 200
                if not self._key_verified:
                    logger.warning("Circle key verification failed: status=%s", resp.status_code)
            except httpx.HTTPError as exc:
                logger.warning("Circle key verification error: %s", exc)
                return False  # transient; retry next time
        return bool(self._key_verified)

    def _entity_secret_ciphertext(self) -> str:
        resp = httpx.get(
            f"{self._base}/v1/w3s/config/entity/publicKey", headers=self._headers(), timeout=15.0
        )
        resp.raise_for_status()
        public_key = load_pem_public_key(resp.json()["data"]["publicKey"].encode())
        ciphertext = public_key.encrypt(
            bytes.fromhex(self._s.circle_entity_secret),
            OAEP(mgf=MGF1(algorithm=SHA256()), algorithm=SHA256(), label=None),
        )
        return base64.b64encode(ciphertext).decode()

    def pay(self, amount_micros: int, memo: str) -> PaymentResult:
        ok, reason = self.available()
        if not ok:
            return PaymentResult(self.name, amount_micros, FAILED, error=reason)
        if not self._verify_key_once():
            return PaymentResult(self.name, amount_micros, FAILED, error="Circle API key verification failed")

        try:
            payload = {
                "idempotencyKey": str(uuid.uuid4()),
                "walletId": self._s.circle_source_wallet_id,
                "tokenId": self._s.circle_token_id,
                "destinationAddress": self._s.circle_wallet_address,
                "amounts": [_format_usdc(amount_micros)],
                "feeLevel": "MEDIUM",
                "entitySecretCiphertext": self._entity_secret_ciphertext(),
            }
            resp = httpx.post(
                f"{self._base}/v1/w3s/developer/transactions/transfer",
                json=payload,
                headers=self._headers(),
                timeout=30.0,
            )
            logger.info("Circle transfer response: status=%s", resp.status_code)
            logger.debug("Circle transfer body: %s", resp.text[:_LOG_BODY_LIMIT])
            resp.raise_for_status()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            detail = str(exc)
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f"HTTP {exc.response.status_code}: {exc.response.text[:_LOG_BODY_LIMIT]}"
            logger.warning("Circle transfer failed: %s", detail)
            return PaymentResult(self.name, amount_micros, FAILED, error=detail)

        body = resp.json()
        data = body.get("data", body)
        state = str(data.get("state", "initiated")).lower()
        if state in _FAILED_STATES:
            return PaymentResult(self.name, amount_micros, FAILED, reference=data.get("id", ""), error=state)
        return PaymentResult(
            rail=self.name,
            amount_micros=amount_micros,
            status=SUBMITTED,
            reference=data.get("id", ""),
        )

    def status(self) -> dict[str, Any]:
        ok, reason = self.available()
        return {"rail": self.name, "available": ok, "detail": reason}


def _format_usdc(amount_micros: int) -> str:
    whole, frac = divmod(amount_micros, 1_000_000)
    return f"{whole}.{frac:06d}".rstrip("0").rstrip(".") or "0"
