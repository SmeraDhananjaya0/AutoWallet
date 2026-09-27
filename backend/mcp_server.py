"""MCP server exposing AutoWallet's paid search to any MCP client.

The ``search_web`` tool runs the full x402 flow against the AutoWallet API:
POST /search -> 402 with payment requirements -> obtain a scoped payment token
-> retry with ``X-PAYMENT: spt <token>`` -> results plus a payment receipt.

    python -m backend.mcp_server           # stdio (Claude Desktop / Claude Code)
    python -m backend.mcp_server --http    # streamable HTTP for remote agents

Requires the API to be running (AUTOWALLET_API_URL, default http://localhost:8000).
Set SPT_ISSUER_KEY to the same value as the API's when the API isn't in DEMO_MODE.
"""

from __future__ import annotations

import argparse
import os
import threading
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server import MCPServer

load_dotenv(Path(__file__).resolve().parent / ".env")  # picks up SPT_ISSUER_KEY

API_URL = os.environ.get("AUTOWALLET_API_URL", "http://localhost:8000").rstrip("/")
AGENT_ID = os.environ.get("AUTOWALLET_MCP_AGENT_ID", "mcp-client")
TOKEN_BUDGET_USD = float(os.environ.get("AUTOWALLET_MCP_TOKEN_BUDGET_USD", "0.10"))
ISSUER_KEY = os.environ.get("SPT_ISSUER_KEY", "")

server = MCPServer(
    name="autowallet",
    instructions=(
        "AutoWallet sells web search per call, priced by query complexity ($0.001-$0.010) and paid "
        "through an x402-style 402 flow. Every search_web call spends real wallet funds."
    ),
)

_token_lock = threading.Lock()
_token: str | None = None


def _get_token(client: httpx.Client, *, fresh: bool = False) -> str:
    global _token
    with _token_lock:
        if _token is None or fresh:
            resp = client.post(
                f"{API_URL}/spt/issue",
                json={"agent_id": AGENT_ID, "tool_scope": "search_web", "max_amount_usd": TOKEN_BUDGET_USD},
                headers={"Authorization": f"Bearer {ISSUER_KEY}"} if ISSUER_KEY else None,
            )
            resp.raise_for_status()
            _token = resp.json()["token"]
        return _token


def paid_search(query: str, client: httpx.Client | None = None) -> dict[str, Any]:
    client = client or httpx.Client(timeout=60.0)
    first = client.post(f"{API_URL}/search", json={"query": query})
    if first.status_code != 402:
        first.raise_for_status()
        return {"results": first.json(), "payment": None}

    requirements = first.json()
    for fresh in (False, True):  # retry once with a new token if the old one is spent/expired
        token = _get_token(client, fresh=fresh)
        paid = client.post(f"{API_URL}/search", json={"query": query}, headers={"X-PAYMENT": f"spt {token}"})
        if paid.status_code == 200:
            h = paid.headers
            return {
                "results": paid.json(),
                "payment": {
                    "amount_usd": float(h.get("x-payment-amount", "0")),
                    "rail": h.get("x-payment-rail"),
                    "reference": h.get("x-payment-reference"),
                    "complexity_score": int(h.get("x-complexity-score", "0")),
                },
            }
        if paid.status_code != 402:
            paid.raise_for_status()
        requirements = paid.json()
    raise RuntimeError(f"Payment was refused: {requirements.get('reason', 'unknown reason')}")


@server.tool(description="Paid web search. Charges $0.001-$0.010 per call via the AutoWallet x402 paywall.")
def search_web(query: str) -> dict[str, Any]:
    return paid_search(query)


@server.tool(description="Current AutoWallet balance and rail status. Free.")
def wallet_status() -> dict[str, Any]:
    resp = httpx.get(f"{API_URL}/wallet/status", timeout=15.0)
    resp.raise_for_status()
    data = resp.json()
    return {
        "balance_usd": data["balance_usd"],
        "rails": {r["rail"]: r["available"] for r in data["rails"]},
        "auto_topup_enabled": data["auto_topup"]["enabled"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    args = parser.parse_args()
    server.run(transport="streamable-http" if args.http else "stdio")


if __name__ == "__main__":
    main()
