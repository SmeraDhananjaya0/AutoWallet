"""Claude agent loop with a paid ``search_web`` tool.

Payment happens inside the tool, before results exist: the router charges the
wallet, and only a successful charge unlocks the search. Claude never hears
"charged" unless money actually moved on a rail.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic

from backend.config import Settings
from backend.ledger import micros_to_usd
from backend.pricing import price_micros
from backend.router import PaymentRouter
from backend.search import search

logger = logging.getLogger("autowallet.agent")

MAX_PAID_CALLS_PER_CHAT = 3
MAX_LOOP_TURNS = 8
FALLBACK_BETA = "server-side-fallback-2026-07-01"

TOOLS = [
    {
        "name": "search_web",
        "description": (
            "Search the web. This is a paid call: before results are returned, the agent wallet is "
            "charged a price based on the query's complexity ($0.001-$0.010). If the payment fails, "
            "the tool returns an error and no results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_wallet_balance",
        "description": "Return the agent wallet's current balance in USD. Free.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

SYSTEM_PROMPT = """You are AutoWallet, an AI agent that pays for its own tools from a wallet.

search_web is paid per call ($0.001-$0.010 depending on query complexity), and the wallet is charged before results come back. Use it when the user's request needs fresh or external information; answer directly when it doesn't. Prefer one well-formed query over several narrow ones. You can make at most 3 paid calls per request.

If search_web returns an error, tell the user the payment or search failed and don't invent results. When you use search results, say what you found and mention roughly what the search cost."""


@dataclass
class AgentResult:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    search_results: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


class Agent:
    def __init__(self, settings: Settings, router: PaymentRouter, client: Any):
        self._s = settings
        self._router = router
        self._client = client

    def _create(self, messages: list[Any]) -> Any:
        params: dict[str, Any] = {
            "model": self._s.claude_model,
            "max_tokens": 16000,
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
            "messages": messages,
            "thinking": {"type": "adaptive"},
        }
        if self._s.claude_fallbacks:
            # Server-side refusal fallback: the API re-runs a declined turn on a fallback model.
            return self._client.beta.messages.create(
                **params, betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"}
            )
        return self._client.messages.create(**params)

    def run(self, user_message: str, history: list[dict[str, Any]] | None = None) -> AgentResult:
        result = AgentResult(text="")
        messages: list[Any] = [*(history or []), {"role": "user", "content": user_message}]
        paid_calls = 0

        for _ in range(MAX_LOOP_TURNS):
            response = self._create(messages)
            # Echo the full content back (thinking blocks must be preserved unchanged).
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                result.text = _text(response.content) or "I can't help with that request."
                return result
            if response.stop_reason != "tool_use":
                result.text = _text(response.content) or "Task completed."
                return result

            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                if block.name == "search_web" and paid_calls >= MAX_PAID_CALLS_PER_CHAT:
                    content, is_error = (
                        f"Limit of {MAX_PAID_CALLS_PER_CHAT} paid calls per request reached. "
                        "Answer with what you have.",
                        True,
                    )
                else:
                    if block.name == "search_web":
                        paid_calls += 1
                    content, is_error = self._run_tool(block.name, dict(block.input or {}), result)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error}
                )
            messages.append({"role": "user", "content": tool_results})

        result.text = _text(response.content) or "Stopped after too many steps."
        return result

    def _run_tool(self, name: str, inputs: dict[str, Any], result: AgentResult) -> tuple[str, bool]:
        if name == "get_wallet_balance":
            return json.dumps({"balance_usd": micros_to_usd(self._router.ledger.balance_micros)}), False
        if name != "search_web":
            return f"Unknown tool: {name}", True

        query = str(inputs.get("query", "")).strip()
        if not query:
            return "query is required", True

        score, amount = price_micros(query)
        outcome = self._router.charge(amount, f"Web search: {query}", metadata={"score": score, "query": query})
        result.events.extend(outcome.events)

        call: dict[str, Any] = {
            "tool": "search_web",
            "reason": f"Web search: {query}",
            "query": query,
            "score": score,
            "amount_usd": micros_to_usd(amount),
            "rail": outcome.rail,
        }
        if not outcome.ok:
            call.update(status="failed", error=outcome.error)
            result.tool_calls.append(call)
            return f"PAYMENT FAILED - no search was run. {outcome.error}", True

        payment = outcome.payment
        call.update(
            status="paid",
            reference=payment.reference,
            simulated=payment.simulated,
            explorer_url=payment.explorer_url,
            transaction_id=outcome.transaction["id"],
        )
        result.tool_calls.append(call)

        found = search(query, brave_api_key=self._s.brave_search_api_key)
        result.search_results.append(found)
        receipt: dict[str, Any] = {
            "charged_usd": micros_to_usd(amount),
            "rail": outcome.rail,
            "reference": payment.reference,
            "balance_after_usd": micros_to_usd(self._router.ledger.balance_micros),
        }
        if outcome.events:
            # Tell Claude about refills so it doesn't report a stale balance.
            receipt["auto_topups"] = [
                {"amount_usd": e["amount_usd"], "status": e["status"]} for e in outcome.events
            ]
        return json.dumps({"payment": receipt, **found}), False


def _text(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


def make_client(settings: Settings) -> anthropic.Anthropic | None:
    if not settings.anthropic_api_key:
        return None
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=5, timeout=120.0)
