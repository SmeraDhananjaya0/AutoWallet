"""Route payments to Stripe or Circle based on amount and policy."""

from __future__ import annotations

import base64
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.padding import MGF1, OAEP
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / "backend" / ".env")

from backend.main import charge_wallet, state, stripe

logger = logging.getLogger("autowallet.payment_router")

ROUTING_THRESHOLD_USD = 0.01


def decide_query_rail(user_message: str) -> dict[str, Any]:
    """Score the query once and pick Stripe vs Circle from total estimated cost."""
    from complexity_scorer import score_query, score_to_price

    score = score_query(user_message)
    total_cost = score_to_price(score)
    rail = "stripe" if total_cost >= ROUTING_THRESHOLD_USD else "circle"
    return {
        "score": score,
        "total_cost_usd": total_cost,
        "rail": rail,
    }


def init_query_payment(decision: dict[str, Any]) -> dict[str, Any]:
    """Charge the full query upfront on Stripe; Circle charges per tool call."""
    if decision["rail"] == "circle":
        return {
            "rail": "circle",
            "amount_usd": decision["total_cost_usd"],
            "status": "deferred",
        }
    return _route_stripe(decision["total_cost_usd"])


def _circle_api_base() -> str:
    override = os.getenv("CIRCLE_API_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    api_key = os.getenv("CIRCLE_API_KEY", "")
    if api_key.startswith(("TEST_API_KEY", "LIVE_API_KEY")):
        return "https://api.circle.com"
    return "https://api-sandbox.circle.com"


def _circle_verify_url(api_key: str) -> str:
    base = _circle_api_base()
    if api_key.startswith(("TEST_API_KEY", "LIVE_API_KEY")):
        return f"{base}/v1/w3s/config/entity"
    return f"{base}/v1/configuration"


def _circle_w3s_transactions_url() -> str:
    """Circle Programmable Wallets transfer create endpoint (W3S transactions API)."""
    return f"{_circle_api_base()}/v1/w3s/developer/transactions/transfer"


def get_fresh_ciphertext() -> str:
    """Fetch Circle's public key and RSA-OAEP-encrypt the entity secret for this request."""
    api_key = os.getenv("CIRCLE_API_KEY", "").strip()
    entity_secret_hex = os.getenv("CIRCLE_ENTITY_SECRET", "").strip()
    if not api_key:
        raise ValueError("CIRCLE_API_KEY not set")
    if not entity_secret_hex:
        raise ValueError("CIRCLE_ENTITY_SECRET not set")

    public_key_url = f"{_circle_api_base()}/v1/w3s/config/entity/publicKey"
    response = httpx.get(
        public_key_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    response.raise_for_status()
    public_key_pem = response.json()["data"]["publicKey"]

    public_key = load_pem_public_key(public_key_pem.encode())
    entity_secret_bytes = bytes.fromhex(entity_secret_hex)
    ciphertext = public_key.encrypt(
        entity_secret_bytes,
        OAEP(
            mgf=MGF1(algorithm=SHA256()),
            algorithm=SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode()


def _verify_circle_api_key(api_key: str) -> bool:
    """Verify Circle API key via the W3S entity or Mint configuration endpoint."""
    config_url = _circle_verify_url(api_key)
    logger.info("Circle configuration check: GET %s", config_url)
    try:
        response = httpx.get(
            config_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        logger.info(
            "Circle configuration response: status=%s body=%s",
            response.status_code,
            response.text,
        )
        if response.status_code == 200:
            logger.info("Circle API key verified")
            return True
        logger.warning(
            "Circle API key verification failed: status=%s body=%s",
            response.status_code,
            response.text,
        )
        return False
    except httpx.HTTPError as exc:
        logger.warning("Circle API key verification failed: %s", exc)
        return False


def _format_usdc_amount(amount_usd: float) -> str:
    return f"{amount_usd:.6f}".rstrip("0").rstrip(".") or "0"


def _circle_transfer_config(
    amount_usd: float,
    recipient_address: str | None,
    entity_secret_ciphertext: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Build W3S transfer payload or return (None, skip_reason)."""
    destination = (recipient_address or os.getenv("CIRCLE_WALLET_ADDRESS", "")).strip()
    if not destination:
        return None, "CIRCLE_WALLET_ADDRESS not set"

    token_id = os.getenv("CIRCLE_TOKEN_ID", "").strip()
    wallet_id = os.getenv("CIRCLE_SOURCE_WALLET_ID", "").strip()

    if not token_id:
        return None, "CIRCLE_TOKEN_ID not set"
    if not wallet_id:
        return None, "CIRCLE_SOURCE_WALLET_ID not set"

    payload: dict[str, Any] = {
        "idempotencyKey": str(uuid.uuid4()),
        "tokenId": token_id,
        "destinationAddress": destination,
        "amounts": [_format_usdc_amount(amount_usd)],
        "feeLevel": "MEDIUM",
        "entitySecretCiphertext": entity_secret_ciphertext,
        "walletId": os.getenv("CIRCLE_SOURCE_WALLET_ID"),
    }

    return payload, None


def route_payment(
    amount_usd: float,
    recipient_address: str | None = None,
    *,
    score: int | None = None,
    force_rail: str | None = None,
) -> dict[str, Any]:
    """Route a payment to Circle (micropayments) or Stripe (>= $0.01)."""
    if force_rail == "circle":
        result = _route_circle(amount_usd, recipient_address)
    elif force_rail == "stripe":
        result = _route_stripe(amount_usd)
    elif amount_usd < ROUTING_THRESHOLD_USD:
        result = _route_circle(amount_usd, recipient_address)
    else:
        result = _route_stripe(amount_usd)

    logger.info(
        "payment_route_decision score=%s amount_usd=%s rail=%s status=%s",
        score,
        amount_usd,
        result.get("rail"),
        result.get("status"),
        extra={
            "score": score,
            "amount_usd": amount_usd,
            "rail": result.get("rail"),
            "status": result.get("status"),
        },
    )
    return result


def _route_circle(amount_usd: float, recipient_address: str | None) -> dict[str, Any]:
    api_key = os.getenv("CIRCLE_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "Circle path skipped: CIRCLE_API_KEY not set (amount_usd=%s)",
            amount_usd,
        )
        return {
            "rail": "circle",
            "amount_usd": amount_usd,
            "status": "skipped",
            "reason": "CIRCLE_API_KEY not set",
        }

    if not _verify_circle_api_key(api_key):
        return {
            "rail": "circle",
            "amount_usd": amount_usd,
            "status": "skipped",
            "reason": "Circle API key verification failed",
        }

    if not os.getenv("CIRCLE_ENTITY_SECRET", "").strip():
        logger.warning(
            "Circle W3S transfer skipped: CIRCLE_ENTITY_SECRET not set (amount_usd=%s)",
            amount_usd,
        )
        return {
            "rail": "circle",
            "amount_usd": amount_usd,
            "status": "skipped",
            "reason": "CIRCLE_ENTITY_SECRET not set",
        }

    try:
        entity_secret_ciphertext = get_fresh_ciphertext()
    except (httpx.HTTPError, ValueError) as exc:
        logger.exception("Circle entity secret encryption failed for amount_usd=%s", amount_usd)
        return {
            "rail": "circle",
            "amount_usd": amount_usd,
            "status": "failed",
            "reason": str(exc),
        }

    payload, skip_reason = _circle_transfer_config(
        amount_usd,
        recipient_address,
        entity_secret_ciphertext,
    )
    if payload is None:
        logger.warning(
            "Circle W3S transfer skipped: %s (amount_usd=%s)",
            skip_reason,
            amount_usd,
        )
        return {
            "rail": "circle",
            "amount_usd": amount_usd,
            "status": "skipped",
            "reason": skip_reason,
        }

    transfer_url = _circle_w3s_transactions_url()

    try:
        log_payload = {**payload, "entitySecretCiphertext": "<redacted>"}
        logger.info("Circle W3S transfer request: POST %s payload=%s", transfer_url, log_payload)
        response = httpx.post(
            transfer_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        logger.info(
            "Circle W3S transfer response: status=%s body=%s",
            response.status_code,
            response.text,
        )
        response.raise_for_status()
        data = response.json().get("data", response.json())
        tx_id = data.get("id", "")
        state_value = str(data.get("state", "pending")).lower()
        return {
            "rail": "circle",
            "amount_usd": amount_usd,
            "status": state_value,
            "tx_id": tx_id,
        }
    except httpx.HTTPError as exc:
        logger.exception("Circle W3S transfer failed for amount_usd=%s", amount_usd)
        detail = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            detail = exc.response.text or detail
        return {
            "rail": "circle",
            "amount_usd": amount_usd,
            "status": "failed",
            "reason": detail,
        }


def _route_stripe(amount_usd: float) -> dict[str, Any]:
    session: dict[str, Any] = {"search_unlocked": False}
    message, records = charge_wallet(
        f"Payment router: ${amount_usd:.4f}",
        amount_usd=amount_usd,
        session=session,
    )

    if message.startswith("PAYMENT FAILED"):
        return {
            "rail": "stripe",
            "amount_usd": amount_usd,
            "status": "failed",
            "reason": message,
        }

    payment_intent_id = records[0].get("stripe_charge_id") if records else None
    return {
        "rail": "stripe",
        "amount_usd": amount_usd,
        "status": "success",
        "payment_intent_id": payment_intent_id,
    }


def _ensure_stripe_customer_for_cli() -> None:
    """Best-effort Stripe setup when running this module directly."""
    if state.get("customer_id"):
        return
    api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not api_key:
        return
    stripe.api_key = api_key
    customer = stripe.Customer.create(name="AutoWallet Agent (payment_router CLI)")
    state["customer_id"] = customer.id
    if state.get("balance_usd") is None:
        state["balance_usd"] = 10.0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    _ensure_stripe_customer_for_cli()

    for amount in [0.001, 0.005, 0.01, 0.08, 0.80]:
        result = route_payment(amount, score=round(amount * 1000))
        print(f"amount_usd={amount} -> {result}")
