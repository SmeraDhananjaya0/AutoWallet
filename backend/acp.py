"""Service manifest describing the paid search tool to other agents.

Served locally at ``/merchant/manifest``. There is no public ACP merchant
registry API to register against, so nothing is sent anywhere.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from backend.pricing import PRICE_PER_POINT_USD

merchant_router = APIRouter()


def build_manifest(base_url: str, chain_id: int, token_address: str, merchant_address: str) -> dict[str, Any]:
    return {
        "name": "search_web",
        "description": "Web search priced by query complexity.",
        "endpoint": f"{base_url}/search",
        "method": "POST",
        "input": {"query": "string"},
        "pricing": {
            "model": "per_call",
            "currency": "usd",
            "min_usd": PRICE_PER_POINT_USD,
            "max_usd": PRICE_PER_POINT_USD * 10,
        },
        "payment": {
            "protocol": "x402-style",
            "schemes": [
                {"scheme": "spt", "issue_url": f"{base_url}/spt/issue", "scope": "search_web"},
                {
                    "scheme": "exact",
                    "network": f"robinhood-chain:{chain_id}",
                    "asset": token_address,
                    "payTo": merchant_address,
                },
            ],
        },
        "mcp": {"transport": "streamable-http", "command": "python -m backend.mcp_server --http"},
    }


@merchant_router.get("/manifest")
def get_manifest(request: Request) -> dict[str, Any]:
    services = request.app.state.services
    chain = services.router.chain
    return build_manifest(
        services.settings.public_base_url, services.settings.chain_id, chain.token_address, chain.merchant_address
    )
