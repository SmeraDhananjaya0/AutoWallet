"""Register this agent as an Agent Commerce Protocol (ACP) merchant endpoint."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import stripe
from dotenv import load_dotenv
from fastapi import APIRouter

load_dotenv(Path(__file__).resolve().parent / "backend" / ".env")

logger = logging.getLogger("autowallet.acp")

ACP_REGISTRY_URL = "https://api.stripe.com/v1/acp/merchants"
_PROJECT_ROOT = Path(__file__).resolve().parent
_MERCHANT_JSON = _PROJECT_ROOT / "merchant.json"

merchant_router = APIRouter()


def _public_base_url() -> str:
    return os.getenv("PUBLIC_ENDPOINT_URL", "http://localhost:8000").rstrip("/")


def build_manifest() -> dict[str, Any]:
    """Build the ACP service manifest for the search_web tool."""
    base = _public_base_url()
    return {
        "name": "search_web",
        "description": (
            "Performs web search and returns structured results. "
            "Priced dynamically by query complexity ($0.001–$0.01)."
        ),
        "endpoint": f"{base}/search",
        "pricing": {
            "model": "per_call",
            "min_usd": 0.001,
            "max_usd": 0.01,
            "currency": "usd",
        },
        "auth": {
            "type": "spt",
            "spt_endpoint": f"{base}/spt/issue",
        },
        "protocol": "x402",
    }


def register_merchant() -> dict[str, Any]:
    """Register the search_web manifest with Stripe ACP or run in local-only mode."""
    manifest = build_manifest()
    api_key = stripe.api_key or os.environ.get("STRIPE_SECRET_KEY", "")

    try:
        response = httpx.post(
            ACP_REGISTRY_URL,
            json=manifest,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        if response.status_code != 200:
            raise httpx.HTTPStatusError(
                f"ACP registry returned {response.status_code}",
                request=response.request,
                response=response,
            )
    except httpx.HTTPError:
        logger.warning("ACP registry unavailable, running in local-only mode")
        return {**manifest, "registered": False}

    data = response.json()
    merchant_id = data.get("merchant_id") or data.get("id")
    if merchant_id:
        _MERCHANT_JSON.write_text(
            json.dumps({"merchant_id": merchant_id}, indent=2) + "\n",
            encoding="utf-8",
        )

    return {**manifest, **data, "registered": True}


@merchant_router.get("/manifest")
def get_manifest() -> dict[str, Any]:
    return build_manifest()


if __name__ == "__main__":
    print(json.dumps(register_merchant(), indent=2))
