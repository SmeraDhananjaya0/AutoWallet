"""Scoped payment tokens: a spending allowance an agent presents to a paid endpoint.

A JWT (HS256, its own signing secret - never the Stripe key) carrying a tool
scope, a spend cap and an expiry. The server tracks cumulative spend per token
(``jti``) so the cap is enforced across calls. This is a stand-in for Stripe's
Shared Payment Tokens in the ACP spec, not an implementation of them.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from backend.ledger import micros_to_usd, usd_to_micros

logger = logging.getLogger("autowallet.spt")

DEFAULT_TTL_SECONDS = 3600


@dataclass
class TokenGrant:
    jti: str
    agent_id: str
    scope: str
    max_micros: int
    expires_at: int
    spent_micros: int = 0

    @property
    def remaining_micros(self) -> int:
        return self.max_micros - self.spent_micros


class MemoryGrantStore:
    """In-process grant storage (single server). ``backend.db.PgGrantStore`` is the shared version."""

    def __init__(self) -> None:
        self._grants: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def insert(self, grant: TokenGrant) -> None:
        with self._lock:
            self._grants[grant.jti] = vars(grant).copy()

    def get(self, jti: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._grants.get(jti)
            return dict(row) if row else None

    def try_spend(self, jti: str, amount_micros: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._grants.get(jti)
            if row is None or row["spent_micros"] + amount_micros > row["max_micros"]:
                return None
            row["spent_micros"] += amount_micros
            return dict(row)

    def refund(self, jti: str, amount_micros: int) -> None:
        with self._lock:
            row = self._grants.get(jti)
            if row:
                row["spent_micros"] = max(0, row["spent_micros"] - amount_micros)


class SptIssuer:
    def __init__(self, secret: str, max_amount_usd: float, store: Any = None):
        if not secret:
            raise ValueError("SPT signing secret is required")
        self._secret = secret
        self._max_micros = usd_to_micros(max_amount_usd)
        self._store = store or MemoryGrantStore()

    def issue(self, agent_id: str, scope: str, max_amount_usd: float, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
        max_micros = usd_to_micros(max_amount_usd)
        if max_micros <= 0:
            raise ValueError("max_amount_usd must be positive")
        if max_micros > self._max_micros:
            raise ValueError(f"max_amount_usd exceeds the per-token cap of ${micros_to_usd(self._max_micros):.2f}")
        grant = TokenGrant(
            jti=uuid.uuid4().hex,
            agent_id=agent_id,
            scope=scope,
            max_micros=max_micros,
            expires_at=int(time.time()) + ttl_seconds,
        )
        self._store.insert(grant)
        token = jwt.encode(
            {
                "jti": grant.jti,
                "sub": agent_id,
                "scope": scope,
                "max_micros": max_micros,
                "exp": grant.expires_at,
            },
            self._secret,
            algorithm="HS256",
        )
        logger.info("spt_issued agent=%s scope=%s max=$%.4f", agent_id, scope, max_amount_usd)
        return token

    def _claims(self, token: str, scope: str) -> tuple[dict[str, Any] | None, str]:
        try:
            claims = jwt.decode(token, self._secret, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return None, "token expired"
        except jwt.PyJWTError:
            return None, "invalid token"
        if claims.get("scope") != scope:
            return None, f"token scope {claims.get('scope')!r} does not cover {scope!r}"
        return claims, "ok"

    def lookup(self, token: str, scope: str) -> tuple[TokenGrant | None, str]:
        claims, reason = self._claims(token, scope)
        if claims is None:
            return None, reason
        row = self._store.get(claims.get("jti", ""))
        if row is None:
            return None, "unknown token"
        return TokenGrant(**row), "ok"

    def try_spend(self, token: str, scope: str, amount_micros: int) -> tuple[TokenGrant | None, str]:
        """Atomically reserve ``amount_micros`` against the token's cap."""
        grant, reason = self.lookup(token, scope)
        if grant is None:
            return None, reason
        row = self._store.try_spend(grant.jti, amount_micros)
        if row is None:
            return None, (
                f"token allowance exhausted: ${micros_to_usd(grant.remaining_micros):.6f} left, "
                f"${micros_to_usd(amount_micros):.6f} needed"
            )
        return TokenGrant(**row), "ok"

    def refund(self, grant: TokenGrant, amount_micros: int) -> None:
        self._store.refund(grant.jti, amount_micros)


# --- HTTP routes -----------------------------------------------------------

spt_router = APIRouter()


class IssueRequest(BaseModel):
    agent_id: str
    tool_scope: str = "search_web"
    max_amount_usd: float = Field(gt=0)
    ttl_seconds: int = Field(default=DEFAULT_TTL_SECONDS, gt=0, le=24 * 3600)


class VerifyRequest(BaseModel):
    token: str
    required_scope: str


def _issuer(request: Request) -> SptIssuer:
    return request.app.state.services.spt


def _authorize_issue(request: Request, authorization: str | None) -> None:
    """Issuing tokens hands out spend from the server's wallet, so it's gated.

    With SPT_ISSUER_KEY set, callers must send ``Authorization: Bearer <key>``.
    Without it, issuing is only open in DEMO_MODE.
    """
    settings = request.app.state.services.settings
    if settings.spt_issuer_key:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip(), settings.spt_issuer_key):
            raise HTTPException(status_code=401, detail="invalid or missing issuer key")
    elif not settings.demo_mode:
        raise HTTPException(status_code=403, detail="token issuing is disabled; set SPT_ISSUER_KEY")


@spt_router.post("/issue")
def post_issue(
    body: IssueRequest, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _authorize_issue(request, authorization)
    try:
        token = _issuer(request).issue(body.agent_id, body.tool_scope, body.max_amount_usd, body.ttl_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"token": token}


@spt_router.post("/verify")
def post_verify(body: VerifyRequest, request: Request) -> dict[str, Any]:
    grant, reason = _issuer(request).lookup(body.token, body.required_scope)
    if grant is None:
        return {"valid": False, "reason": reason}
    return {"valid": True, "remaining_usd": micros_to_usd(grant.remaining_micros), "expires_at": grant.expires_at}
