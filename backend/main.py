"""AutoWallet API.

Run from the repository root (so there is exactly one copy of every module):

    pip install -r backend/requirements.txt
    uvicorn backend.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import anthropic
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.acp import merchant_router
from backend.agent import Agent, make_client
from backend.config import Settings, get_settings
from backend.ledger import Ledger, micros_to_usd, usd_to_micros
from backend.rails import CircleRail, RobinhoodChainRail, StripeRail
from backend.router import PaymentRouter
from backend.search import Searcher, SearchError, make_reporter, make_searcher
from backend.spt import SptIssuer, spt_router
from backend.trading import (
    ChainlinkPrices,
    DemoExchange,
    FixedPrices,
    MemoryTradingStore,
    PgTradingStore,
    PriceError,
    TradingError,
    TradingService,
)
from backend.treasury import AutoTopUp, RobinhoodCryptoClient, SimulatedRobinhoodCrypto
from backend.x402 import PaymentReceipt, ReplayGuard, require_payment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autowallet")


class MemorySessionStore:
    """In-process chat history; ``backend.db.PgSessionStore`` is the shared version."""

    def __init__(self) -> None:
        self._sessions: dict[str, list[dict[str, Any]]] = {}

    def get(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._sessions.get(session_id, []))

    def save(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        self._sessions[session_id] = list(messages)


@dataclass
class Services:
    settings: Settings
    ledger: Ledger
    router: PaymentRouter
    topup: AutoTopUp
    spt: SptIssuer
    replay_guard: Any
    agent: Agent | None
    trading: TradingService
    searcher: Searcher
    search_provider: str
    sessions: Any


def build_services(settings: Settings, *, anthropic_client: Any = None, setup_stripe: bool = True) -> Services:
    initial = usd_to_micros(settings.initial_balance_usd)
    stripe_rail = StripeRail(settings)

    if settings.database_url:
        # Shared state: every server instance reads and writes the same database.
        from backend.db import Database, PgGrantStore, PgLedger, PgReplayGuard, PgSessionStore

        db = Database(settings.database_url)
        ledger: Any = PgLedger(db, initial)
        grant_store: Any = PgGrantStore(db)
        replay_guard: Any = PgReplayGuard(db)
        sessions: Any = PgSessionStore(db)
        # Tokens must verify on every instance, so the signing secret is shared too.
        spt_secret = os.environ.get("SPT_SIGNING_SECRET") or db.get_meta("spt_signing_secret")
        if not spt_secret:
            spt_secret = settings.spt_signing_secret
            db.set_meta("spt_signing_secret", spt_secret)
        if setup_stripe:
            stripe_rail.setup(db.get_meta("stripe_customer_id"))
            if stripe_rail.customer_id:
                db.set_meta("stripe_customer_id", stripe_rail.customer_id)
    else:
        ledger = Ledger(initial, db_path=settings.ledger_db_path or None)
        grant_store = None
        replay_guard = ReplayGuard()
        sessions = MemorySessionStore()
        spt_secret = settings.spt_signing_secret
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

    # Demo trading: play-money stock tokens on testnet, priced from Chainlink on mainnet.
    prices: Any = ChainlinkPrices(settings.price_rpc_url) if settings.price_source == "chainlink" else FixedPrices()
    exchange = DemoExchange(chain, settings.demo_exchange_address, settings.demo_stock_tokens)
    trading_store: Any = PgTradingStore(db) if settings.database_url else MemoryTradingStore()
    trading = TradingService(ledger, prices, exchange, trading_store)
    topup.portfolio_funder = trading.sell_for_funding
    router = PaymentRouter(
        ledger,
        stripe=stripe_rail,
        chain=chain,
        circle=circle,
        micropayment_rail=settings.micropayment_rail,
        topup=topup,
    )

    client = anthropic_client if anthropic_client is not None else make_client(settings)
    searcher, search_provider = make_searcher(settings, client)
    reporter = make_reporter(search_provider, settings, client)
    agent = Agent(settings, router, client, searcher, reporter, trading) if client is not None else None

    return Services(
        settings=settings,
        ledger=ledger,
        router=router,
        topup=topup,
        spt=SptIssuer(spt_secret, settings.spt_max_amount_usd, grant_store),
        replay_guard=replay_guard,
        agent=agent,
        trading=trading,
        searcher=searcher,
        search_provider=search_provider,
        sessions=sessions,
    )


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "services", None) is None:
            app.state.services = build_services(get_settings())
        svc: Services = app.state.services
        s = svc.settings
        logger.info(
            "AutoWallet ready | balance $%.2f | chain=%s (%s) | robinhood=%s | stripe=%s | circle=%s | claude=%s | search=%s",
            micros_to_usd(svc.ledger.balance_micros),
            s.chain_mode,
            s.chain_id,
            s.crypto_mode,
            svc.router.stripe.available()[1],
            svc.router.circle.available()[1],
            s.claude_model if svc.agent else "not configured",
            svc.search_provider,
        )
        yield

    app = FastAPI(title="AutoWallet API", lifespan=lifespan)
    app.state.services = services
    init_lock = threading.Lock()

    @app.middleware("http")
    async def ensure_services(request: Request, call_next):
        # Serverless platforms may skip lifespan events; build services on first use instead.
        if getattr(app.state, "services", None) is None:
            def build() -> None:
                with init_lock:
                    if getattr(app.state, "services", None) is None:
                        app.state.services = build_services(get_settings())

            await run_in_threadpool(build)
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:5174"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    api = APIRouter()
    api.include_router(spt_router, prefix="/spt")
    api.include_router(merchant_router, prefix="/merchant")
    _register_routes(api)
    # Served at the root (local dev, MCP server, other agents) and under /api
    # (the hosted frontend; on Vercel, /api/* is routed to this function).
    app.include_router(api)
    app.include_router(api, prefix="/api")
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


def _register_routes(app: APIRouter) -> None:
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
        history = svc.sessions.get(session_id)
        try:
            result = svc.agent.run(text, history, session_id=session_id)
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
        svc.sessions.save(session_id, history)

        return {
            "response": result.text,
            "tool_calls": result.tool_calls,
            "search_results": result.search_results,
            "reports": result.reports,
            "trade_proposals": result.trade_proposals,
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
            "search_provider": svc.search_provider,
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
        try:
            found = svc.searcher(body.query)
        except SearchError as exc:
            return JSONResponse(status_code=502, content={"error": str(exc)}, headers=paid.headers())
        return JSONResponse(content=found, headers=paid.headers())

    # --- demo trading (testnet, play money) -------------------------------

    @app.get("/trading/quotes")
    def get_quotes(request: Request) -> list[dict[str, Any]]:
        return svc_of(request).trading.quotes()

    @app.get("/portfolio")
    def get_portfolio(request: Request) -> dict[str, Any]:
        return svc_of(request).trading.portfolio()

    @app.get("/trading/rules")
    def get_rules(request: Request) -> dict[str, Any]:
        return svc_of(request).trading.rules().to_dict()

    @app.put("/trading/rules")
    def put_rules(body: dict[str, Any], request: Request) -> dict[str, Any]:
        try:
            return svc_of(request).trading.set_rules(body).to_dict()
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/trades")
    def get_trades(request: Request) -> list[dict[str, Any]]:
        return svc_of(request).trading.list()

    @app.post("/trades/{trade_id}/approve")
    def post_approve(trade_id: str, request: Request) -> dict[str, Any]:
        svc = svc_of(request)
        try:
            proposal = svc.trading.approve(trade_id)
        except TradingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"proposal": proposal, "portfolio": svc.trading.portfolio(), **_wallet_snapshot(svc)}

    @app.post("/trades/{trade_id}/reject")
    def post_reject(trade_id: str, request: Request) -> dict[str, Any]:
        try:
            return {"proposal": svc_of(request).trading.reject(trade_id)}
        except TradingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
