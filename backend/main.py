"""AutoWallet API.

Run from the repository root (so there is exactly one copy of every module):

    pip install -r backend/requirements.txt
    uvicorn backend.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import anthropic
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.acp import merchant_router
from backend.agent import Agent, make_client
from backend.config import Settings, get_settings
from backend.ledger import Ledger, micros_to_usd, usd_to_micros
from backend.rails import CircleRail, RobinhoodChainRail, StripeRail
from backend.router import PaymentRouter
from backend.search import search
from backend.spt import SptIssuer, spt_router
from backend.treasury import AutoTopUp, RobinhoodCryptoClient, SimulatedRobinhoodCrypto
from backend.x402 import PaymentReceipt, ReplayGuard, require_payment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autowallet")


@dataclass
class Services:
    settings: Settings
    ledger: Ledger
    router: PaymentRouter
    topup: AutoTopUp
    spt: SptIssuer
    replay_guard: ReplayGuard
    agent: Agent | None
    chat_sessions: dict[str, list[dict[str, Any]]]


def build_services(settings: Settings, *, anthropic_client: Any = None, setup_stripe: bool = True) -> Services:
    ledger = Ledger(usd_to_micros(settings.initial_balance_usd), db_path=settings.ledger_db_path or None)

    stripe_rail = StripeRail(settings)
    if setup_stripe:
        stripe_rail.setup()
    chain = RobinhoodChainRail(settings)
    circle = CircleRail(settings)

    if settings.crypto_mode == "live":
        crypto = RobinhoodCryptoClient(
            settings.crypto_api_key, settings.crypto_private_key_b64, settings.crypto_base_url
        )
    else:
        crypto = SimulatedRobinhoodCrypto()

    topup = AutoTopUp(settings, ledger, crypto, chain)
    router = PaymentRouter(
        ledger,
        stripe=stripe_rail,
        chain=chain,
        circle=circle,
        micropayment_rail=settings.micropayment_rail,
        topup=topup,
    )

    client = anthropic_client if anthropic_client is not None else make_client(settings)
    agent = Agent(settings, router, client) if client is not None else None

    return Services(
        settings=settings,
        ledger=ledger,
        router=router,
        topup=topup,
        spt=SptIssuer(settings.spt_signing_secret, settings.spt_max_amount_usd),
        replay_guard=ReplayGuard(),
        agent=agent,
        chat_sessions={},
    )


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "services", None) is None:
            app.state.services = build_services(get_settings())
        svc: Services = app.state.services
        s = svc.settings
        logger.info(
            "AutoWallet ready | balance $%.2f | chain=%s (%s) | robinhood=%s | stripe=%s | circle=%s | claude=%s",
            micros_to_usd(svc.ledger.balance_micros),
            s.chain_mode,
            s.chain_id,
            s.crypto_mode,
            svc.router.stripe.available()[1],
            svc.router.circle.available()[1],
            s.claude_model if svc.agent else "not configured",
        )
        yield

    app = FastAPI(title="AutoWallet API", lifespan=lifespan)
    app.state.services = services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:5174"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(spt_router, prefix="/spt")
    app.include_router(merchant_router, prefix="/merchant")
    _register_routes(app)
    return app


# --- request / response models ----------------------------------------------


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)


class AmountRequest(BaseModel):
    amount_usd: float | None = Field(default=None, gt=0)


class SetBalanceRequest(BaseModel):
    balance_usd: float = Field(ge=0)


def _wallet_snapshot(svc: Services) -> dict[str, Any]:
    micros = svc.ledger.balance_micros
    return {
        "balance_micros": micros,
        "balance_usd": micros_to_usd(micros),
        "balance_cents": micros // 10_000,
        "transactions": svc.ledger.transactions(),
    }


def _register_routes(app: FastAPI) -> None:
    def svc_of(request: Request) -> Services:
        return request.app.state.services

    @app.post("/chat")
    def post_chat(body: ChatRequest, request: Request) -> dict[str, Any]:
        svc = svc_of(request)
        text = body.message.strip()
        if not text:
            raise HTTPException(status_code=400, detail="message is required")
        if svc.agent is None:
            raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")

        session_id = body.session_id or str(uuid.uuid4())
        history = list(svc.chat_sessions.get(session_id, []))
        try:
            result = svc.agent.run(text, history)
        except anthropic.APIStatusError as exc:
            logger.exception("Claude API error on /chat")
            raise HTTPException(
                status_code=502 if exc.status_code >= 500 else 400,
                detail=f"Claude API error ({exc.status_code}): {exc.message}",
            ) from None
        except anthropic.APIConnectionError:
            logger.exception("Claude API connection error on /chat")
            raise HTTPException(status_code=502, detail="Could not reach the Claude API") from None

        history += [{"role": "user", "content": text}, {"role": "assistant", "content": result.text}]
        svc.chat_sessions[session_id] = history

        return {
            "response": result.text,
            "tool_calls": result.tool_calls,
            "search_results": result.search_results,
            "events": result.events,
            "session_id": session_id,
            **_wallet_snapshot(svc),
        }

    @app.get("/wallet/balance")
    def get_balance(request: Request) -> dict[str, Any]:
        snap = _wallet_snapshot(svc_of(request))
        snap.pop("transactions")
        return snap

    @app.get("/wallet/status")
    def get_wallet_status(request: Request) -> dict[str, Any]:
        svc = svc_of(request)
        return {
            "balance_usd": micros_to_usd(svc.ledger.balance_micros),
            "rails": svc.router.status(),
            "micropayment_rail": svc.settings.micropayment_rail,
            "auto_topup": svc.topup.status(),
            "demo_mode": svc.settings.demo_mode,
            "agent_configured": svc.agent is not None,
            "claude_model": svc.settings.claude_model,
        }

    @app.post("/wallet/topup")
    def post_card_topup(request: Request) -> dict[str, Any]:
        """Add funds by charging the Stripe test card."""
        svc = svc_of(request)
        amount = usd_to_micros(svc.settings.card_topup_usd)
        result = svc.router.stripe.charge_card(amount, "AutoWallet card top-up")
        if not result.ok:
            raise HTTPException(status_code=502, detail=f"Card top-up failed: {result.error}")
        svc.ledger.credit(amount, "Card top-up", rail="stripe", reference=result.reference)
        return _wallet_snapshot(svc)

    @app.post("/wallet/topup/robinhood")
    def post_robinhood_topup(request: Request, body: AmountRequest | None = None) -> Any:
        """Run the Robinhood treasury top-up now (same path auto top-up uses)."""
        svc = svc_of(request)
        amount = usd_to_micros(body.amount_usd) if body and body.amount_usd else None
        event = svc.topup.run_now(amount)
        payload = {"event": event, **_wallet_snapshot(svc)}
        if event["status"] != "completed":
            return JSONResponse(status_code=502, content=payload)
        return payload

    @app.get("/transactions")
    def get_transactions(request: Request) -> list[dict[str, Any]]:
        return svc_of(request).ledger.transactions()

    @app.post("/search")
    def post_search(
        body: SearchRequest, request: Request, x_payment: str | None = Header(default=None)
    ) -> Any:
        svc = svc_of(request)
        paid = require_payment(svc, x_payment, body.query)
        if not isinstance(paid, PaymentReceipt):
            return paid
        found = search(body.query, brave_api_key=svc.settings.brave_search_api_key)
        return JSONResponse(content=found, headers=paid.headers())

    @app.get("/health")
    def get_health(request: Request) -> dict[str, Any]:
        svc = svc_of(request)
        return {
            "status": "ok",
            "rails": {r["rail"]: r["available"] for r in svc.router.status()},
            "chain_mode": svc.settings.chain_mode,
            "robinhood_crypto_mode": svc.settings.crypto_mode,
            "stripe_min_charge_usd": svc.settings.stripe_min_charge_usd,
            "agent_configured": svc.agent is not None,
        }

    # --- demo helpers (DEMO_MODE=true) -------------------------------------

    def require_demo(svc: Services) -> None:
        if not svc.settings.demo_mode:
            raise HTTPException(status_code=404, detail="Not found")

    @app.post("/demo/set-balance")
    def post_set_balance(body: SetBalanceRequest, request: Request) -> dict[str, Any]:
        """Force the balance, e.g. to stage the 'runs out mid-task' auto top-up moment."""
        svc = svc_of(request)
        require_demo(svc)
        svc.ledger.set_balance(usd_to_micros(Decimal(str(body.balance_usd))))
        return _wallet_snapshot(svc)

    @app.post("/demo/external-payment")
    def post_external_payment(request: Request, body: AmountRequest | None = None) -> dict[str, Any]:
        """Simulated chain only: mint a tx hash that pays the merchant, for trying `X-PAYMENT: tx <hash>`."""
        svc = svc_of(request)
        require_demo(svc)
        chain = svc.router.chain
        if not chain.simulated:
            raise HTTPException(status_code=400, detail="Only available with ROBINHOOD_CHAIN_MODE=simulated")
        amount = body.amount_usd if body and body.amount_usd else 0.01
        result = chain.simulate_external_payment(usd_to_micros(amount))
        return {"tx_hash": result.reference, "amount_usd": micros_to_usd(result.amount_micros)}


app = create_app()
