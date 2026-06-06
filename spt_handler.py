"""Issue and verify Stripe Purchase Tokens (SPT) for agent commerce."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import jwt
import stripe
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("autowallet.spt")

# Shared with backend.main — do not call stripe.api_key = ... here.
_token_cache: dict[str, dict[str, Any]] = {}

spt_router = APIRouter()


class IssueSptRequest(BaseModel):
    agent_id: str
    tool_scope: str
    max_amount_usd: float


class VerifySptRequest(BaseModel):
    token: str
    required_scope: str


class IssueSptResponse(BaseModel):
    token: str


class VerifySptResponse(BaseModel):
    valid: bool


def _jwt_secret() -> str:
    return stripe.api_key or os.environ.get("STRIPE_SECRET_KEY", "")


def issue_spt(agent_id: str, tool_scope: str, max_amount_usd: float) -> str:
    """Create a tool-scoped purchase token (JWT stand-in for Stripe SPT / ACP)."""
    secret = _jwt_secret()
    if not secret:
        raise ValueError("STRIPE_SECRET_KEY is not configured")

    now = datetime.now(timezone.utc)
    payload = {
        "agent_id": agent_id,
        "tool_scope": tool_scope,
        "max_amount_usd": max_amount_usd,
        "exp": int(now.timestamp()) + 3600,
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    _token_cache[token] = {
        "agent_id": agent_id,
        "tool_scope": tool_scope,
        "max_amount_usd": max_amount_usd,
        "issued_at": now.isoformat(),
    }
    logger.info(
        "spt_issued agent_id=%s tool_scope=%s max_amount_usd=%s",
        agent_id,
        tool_scope,
        max_amount_usd,
    )
    return token


def verify_spt(token: str, required_scope: str) -> bool:
    """Validate token signature, expiry, and tool scope. Never raises."""
    secret = _jwt_secret()
    if not secret or not token:
        return False
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return False
    return payload.get("tool_scope") == required_scope


@spt_router.post("/issue", response_model=IssueSptResponse)
def post_issue_spt(body: IssueSptRequest) -> IssueSptResponse:
    try:
        token = issue_spt(body.agent_id, body.tool_scope, body.max_amount_usd)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return IssueSptResponse(token=token)


@spt_router.post("/verify", response_model=VerifySptResponse)
def post_verify_spt(body: VerifySptRequest) -> VerifySptResponse:
    valid = verify_spt(body.token, body.required_scope)
    return VerifySptResponse(valid=valid)
