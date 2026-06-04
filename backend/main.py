# conda activate base (or your env)
# pip install -r requirements.txt
# create .env from .env.example and fill in keys
# uvicorn main:app --reload --port 8000

from __future__ import annotations

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

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
TEST_PAYMENT_METHOD = "pm_card_visa"

state: dict[str, Any] = {
    "balance_cents": 1000,
    "customer_id": None,
    "transactions": [],
}

CLAUDE_TOOLS = [
    {
        "name": "charge_wallet",
        "description": "Charge the agent's wallet to pay for a tool",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount_cents": {
                    "type": "integer",
                    "description": "Amount to charge in cents",
                },
                "reason": {
                    "type": "string",
                    "description": "Human-readable reason for the charge",
                },
            },
            "required": ["amount_cents", "reason"],
        },
    },
    {
        "name": "search_web",
        "description": "Search the web for information. Costs 1 cent per call.",
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

SYSTEM_PROMPT = """You are AutoWallet, an AI agent that completes user tasks autonomously.

You have a prepaid wallet. Use tools to pay for capabilities:
- charge_wallet: pay a specific amount (in cents) for a tool or service
- search_web: search the web (costs 1 cent per call; charges the wallet automatically)

Before spending, consider whether the user's request requires paid tools. For research questions, use search_web.
When you finish, reply with a clear, helpful summary for the user."""

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


def charge_wallet(amount_cents: int, reason: str) -> tuple[str, dict[str, Any] | None]:
    """Charge wallet via Stripe. Returns (message_for_claude, tool_call_record or None)."""
    if amount_cents <= 0:
        return "amount_cents must be a positive integer.", None
    if state["balance_cents"] < amount_cents:
        return (
            f"Insufficient balance: have {state['balance_cents']} cents, need {amount_cents}.",
            None,
        )

    try:
        intent = create_and_confirm_payment(amount_cents)
    except stripe.StripeError as exc:
        return f"Stripe payment failed: {exc.user_message or str(exc)}", None

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
    return (
        f"Successfully charged {amount_cents} cents for: {reason}. "
        f"Remaining balance: {state['balance_cents']} cents.",
        record,
    )


def search_web(query: str) -> tuple[str, dict[str, Any] | None]:
    amount_cents = 1
    reason = f"Web search: {query}"
    message, record = charge_wallet(amount_cents, reason)
    if record is None:
        return message, None

    mock = (
        f"Mock search results for '{query}': Several relevant results found "
        "including recent news, analysis, and data points related to the topic."
    )
    record["tool"] = "search_web"
    return f"{message}\n\n{mock}", record


def run_tool(name: str, inputs: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    if name == "charge_wallet":
        return charge_wallet(
            int(inputs.get("amount_cents", 0)),
            str(inputs.get("reason", "")),
        )
    if name == "search_web":
        return search_web(str(inputs.get("query", "")))
    return f"Unknown tool: {name}", None


def extract_text(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def run_claude_loop(user_message: str) -> tuple[str, list[dict[str, Any]]]:
    if not anthropic_client:
        raise HTTPException(status_code=503, detail="Anthropic client not configured")

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    tool_calls_log: list[dict[str, Any]] = []
    final_text = ""

    while True:
        response = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=CLAUDE_TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

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
            result_text, record = run_tool(block.name, block.input)
            if record:
                tool_calls_log.append(record)
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                }
            )

        messages.append({"role": "user", "content": tool_result_blocks})

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

    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    yield

    anthropic_client = None


app = FastAPI(title="AutoWallet API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
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

    response_text, tool_calls = run_claude_loop(text)
    return ChatResponse(
        response=response_text,
        tool_calls=tool_calls,
        transactions=state["transactions"],
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
