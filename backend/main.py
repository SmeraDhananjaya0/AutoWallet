# conda activate base (or your env)
# pip install -r requirements.txt
# create .env from .env.example and fill in keys
# uvicorn main:app --reload --port 8000

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import anthropic
import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autowallet")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
TEST_PAYMENT_METHOD = "pm_card_visa"
CHARGE_AMOUNT_CENTS = 50  # Stripe minimum for USD
MAX_TOOL_CALLS_PER_CHAT = 3

state: dict[str, Any] = {
    "balance_cents": 1000,
    "customer_id": None,
    "transactions": [],
}

CLAUDE_TOOLS = [
    {
        "name": "charge_wallet",
        "description": (
            "Charge the wallet exactly $0.50 (50 cents) before a paid action. "
            "For web search, pass query to charge and search in one step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Human-readable reason for the charge",
                },
                "query": {
                    "type": "string",
                    "description": "Optional search query; if set, runs search after charging",
                },
            },
            "required": ["reason"],
        },
    },
    {
        "name": "search_web",
        "description": (
            "Search the web after charge_wallet succeeded in this request. "
            "Costs $0.50 per call (paid via charge_wallet). Does not charge again."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
            },
            "required": ["query"],
        },
    },
]

SYSTEM_PROMPT = """You are AutoWallet, an AI agent with a wallet. To search the web, you must call charge_wallet, then call search_web. Each search costs $0.50 (50 cents) via charge_wallet — never more than $0.50 per search. Never call charge_wallet more than 3 times per request. Always complete the search only after a successful charge.

If a tool returns PAYMENT FAILED, do not call search_web, do not invent search results, and tell the user the charge failed.

For research tasks: first call charge_wallet (reason describing the search). Then call search_web with the query. You may pass query on charge_wallet to charge and search in one step. Summarize results for the user when done."""

anthropic_client: anthropic.Anthropic | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_transaction_id() -> str:
    return f"txn_{uuid.uuid4().hex[:12]}"


def stripe_charge_id_from_intent(intent: stripe.PaymentIntent) -> str:
    charge = intent.latest_charge
    if charge is None:
        return intent.id
    if isinstance(charge, str):
        return charge
    return getattr(charge, "id", str(charge))


def create_and_confirm_payment(amount_cents: int) -> stripe.PaymentIntent:
    if not state["customer_id"]:
        raise HTTPException(status_code=503, detail="Stripe customer not initialized")
    return stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        customer=state["customer_id"],
        payment_method=TEST_PAYMENT_METHOD,
        confirm=True,
        off_session=True,
    )


def record_transaction(
    *,
    reason: str,
    amount_cents: int,
    stripe_charge_id: str,
) -> dict[str, Any]:
    txn = {
        "id": new_transaction_id(),
        "timestamp": utc_now_iso(),
        "reason": reason,
        "amount_cents": amount_cents,
        "stripe_charge_id": stripe_charge_id,
    }
    state["transactions"].insert(0, txn)
    return txn


def format_stripe_error(exc: stripe.StripeError) -> str:
    detail = getattr(exc, "user_message", None) or str(exc)
    code = getattr(exc, "code", None)
    code_part = f" (code: {code})" if code else ""
    return (
        "PAYMENT FAILED — STOP. Do not call search_web. Do not fabricate or guess search results. "
        f"Stripe rejected the $0.50 charge{code_part}: {detail}. "
        "Tell the user the wallet charge failed and you cannot complete the paid search."
    )


def mock_search_result(query: str) -> str:
    return (
        f"Mock search results for '{query}': Several relevant results found "
        "including recent news, analysis, and data points related to the topic."
    )


def charge_wallet(
    reason: str,
    *,
    query: str | None = None,
    session: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Always charge exactly 50 cents ($0.50); ignore any amount Claude sends."""
    amount_cents = CHARGE_AMOUNT_CENTS
    reason = reason.strip() or "Wallet charge"

    if state["balance_cents"] < amount_cents:
        session["search_unlocked"] = False
        return (
            "PAYMENT FAILED — STOP. Do not call search_web. Do not fabricate search results. "
            f"Insufficient balance: have {state['balance_cents']} cents, need {amount_cents} ($0.50). "
            "Tell the user the wallet does not have enough funds.",
            [],
        )

    try:
        intent = create_and_confirm_payment(amount_cents)
    except stripe.StripeError as exc:
        logger.exception("Stripe charge failed for reason=%s", reason)
        session["search_unlocked"] = False
        return format_stripe_error(exc), []

    charge_id = stripe_charge_id_from_intent(intent)
    state["balance_cents"] -= amount_cents
    record_transaction(
        reason=reason,
        amount_cents=amount_cents,
        stripe_charge_id=charge_id,
    )
    record = {
        "tool": "charge_wallet",
        "reason": reason,
        "amount_cents": amount_cents,
        "stripe_charge_id": charge_id,
    }
    message = (
        f"Successfully charged $0.50 ({amount_cents} cents) for: {reason}. "
        f"Remaining balance: {state['balance_cents']} cents."
    )

    if query and query.strip():
        search_text = mock_search_result(query.strip())
        session["search_unlocked"] = False
        message = f"{message}\n\n{search_text}"
        return message, [
            record,
            {
                "tool": "search_web",
                "reason": f"Web search: {query.strip()}",
                "amount_cents": 0,
                "stripe_charge_id": charge_id,
            },
        ]

    session["search_unlocked"] = True
    return message, [record]


def search_web(query: str, *, session: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    query = query.strip()
    if not query:
        return "query is required.", []

    if not session.get("search_unlocked"):
        return (
            "Error: call charge_wallet before search_web. "
            "Each search costs $0.50 via charge_wallet first.",
            [],
        )

    session["search_unlocked"] = False
    return mock_search_result(query), [
        {
            "tool": "search_web",
            "reason": f"Web search: {query}",
            "amount_cents": 0,
            "stripe_charge_id": "",
        }
    ]


def run_tool(
    name: str,
    inputs: dict[str, Any],
    *,
    session: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    if name == "charge_wallet":
        return charge_wallet(
            str(inputs.get("reason", "")),
            query=inputs.get("query"),
            session=session,
        )
    if name == "search_web":
        return search_web(str(inputs.get("query", "")), session=session)
    return f"Unknown tool: {name}", []


def extract_text(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def serialize_assistant_content(content: list[Any]) -> list[dict[str, Any]]:
    """Plain dicts for the next Messages API turn (avoids SDK object serialization issues)."""
    serialized: list[dict[str, Any]] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            serialized.append({"type": "text", "text": block.text})
        elif block_type == "tool_use":
            serialized.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
    return serialized


def tool_input_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if hasattr(raw, "model_dump"):
        return raw.model_dump()
    return {}


def run_claude_loop(user_message: str) -> tuple[str, list[dict[str, Any]]]:
    if not anthropic_client:
        raise HTTPException(status_code=503, detail="Anthropic client not configured")

    # Fresh conversation per HTTP request — no shared history between /chat calls.
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    tool_calls_log: list[dict[str, Any]] = []
    session: dict[str, Any] = {
        "tool_call_count": 0,
        "search_unlocked": False,
    }
    final_text = ""
    tool_limit_reached = False

    while True:
        logger.info(
            "Claude request (tool_calls=%d, messages=%d)",
            session["tool_call_count"],
            len(messages),
        )
        response = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=CLAUDE_TOOLS,
            messages=messages,
        )

        messages.append(
            {"role": "assistant", "content": serialize_assistant_content(response.content)}
        )

        if response.stop_reason == "end_turn":
            final_text = extract_text(response.content)
            break

        if response.stop_reason != "tool_use":
            final_text = extract_text(response.content) or "No response from agent."
            break

        tool_result_blocks: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue

            if session["tool_call_count"] >= MAX_TOOL_CALLS_PER_CHAT:
                tool_limit_reached = True
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": (
                            f"Maximum {MAX_TOOL_CALLS_PER_CHAT} tool calls per request reached. "
                            "Respond to the user with what you have."
                        ),
                    }
                )
                continue

            session["tool_call_count"] += 1
            result_text, records = run_tool(
                block.name,
                tool_input_dict(block.input),
                session=session,
            )
            tool_calls_log.extend(records)
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                }
            )

        messages.append({"role": "user", "content": tool_result_blocks})

        if tool_limit_reached:
            follow_up = anthropic_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=CLAUDE_TOOLS,
                messages=messages,
            )
            final_text = extract_text(follow_up.content) or "Task completed (tool limit reached)."
            break

    if not final_text:
        final_text = "Task completed."

    return final_text, tool_calls_log


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global anthropic_client

    if not stripe.api_key:
        raise RuntimeError("STRIPE_SECRET_KEY is required (set in .env)")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is required (set in .env)")

    customer = stripe.Customer.create(name="AutoWallet Agent")
    state["customer_id"] = customer.id
    state["balance_cents"] = 1000
    state["transactions"] = []
    logger.info("Stripe customer created: %s (balance: $10.00)", customer.id)

    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    logger.info("AutoWallet API ready")

    yield

    anthropic_client = None


app = FastAPI(title="AutoWallet API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    response: str
    tool_calls: list[dict[str, Any]]
    transactions: list[dict[str, Any]]
    balance_cents: int


class BalanceResponse(BaseModel):
    balance_cents: int


class TopUpResponse(BaseModel):
    balance_cents: int
    transactions: list[dict[str, Any]]


@app.post("/chat", response_model=ChatResponse)
def post_chat(body: ChatRequest) -> ChatResponse:
    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message is required")

    try:
        response_text, tool_calls = run_claude_loop(text)
    except anthropic.APIError:
        logger.exception("Anthropic API error on /chat")
        raise HTTPException(status_code=502, detail="Claude API request failed") from None
    except stripe.StripeError:
        logger.exception("Stripe error on /chat")
        raise HTTPException(status_code=502, detail="Payment processing failed") from None
    except Exception:
        logger.exception("Unhandled error on /chat")
        raise

    return ChatResponse(
        response=response_text,
        tool_calls=tool_calls,
        transactions=list(state["transactions"]),
        balance_cents=state["balance_cents"],
    )


@app.get("/wallet/balance", response_model=BalanceResponse)
def get_balance() -> BalanceResponse:
    return BalanceResponse(balance_cents=state["balance_cents"])


@app.post("/wallet/topup", response_model=TopUpResponse)
def post_topup() -> TopUpResponse:
    amount_cents = 1000
    try:
        intent = create_and_confirm_payment(amount_cents)
    except stripe.StripeError as exc:
        raise HTTPException(
            status_code=502,
            detail=exc.user_message or str(exc),
        ) from exc

    charge_id = stripe_charge_id_from_intent(intent)
    state["balance_cents"] += amount_cents
    record_transaction(
        reason="Wallet top-up",
        amount_cents=-amount_cents,
        stripe_charge_id=charge_id,
    )
    return TopUpResponse(
        balance_cents=state["balance_cents"],
        transactions=state["transactions"],
    )


@app.get("/transactions")
def get_transactions() -> list[dict[str, Any]]:
    return state["transactions"]
