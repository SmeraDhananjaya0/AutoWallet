"""Claude agent loop with paid tools.

Payment happens inside each paid tool, before anything is delivered: the router
charges the wallet, and only a successful charge unlocks the work. Claude never
hears "charged" unless money actually moved on a rail.

Paid tools:
  search_web            $0.001-$0.010 by complexity -> settles on Robinhood Chain
  deep_research_report  flat REPORT_PRICE_USD ($0.75) -> at/above $0.50, settles on Stripe
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from backend.config import Settings
from backend.ledger import micros_to_usd, usd_to_micros
from backend.pricing import price_micros
from backend.router import ChargeOutcome, PaymentRouter
from backend.search import Reporter, Searcher, SearchError, mock_report, mock_search

logger = logging.getLogger("autowallet.agent")

MAX_PAID_SEARCHES_PER_CHAT = 3
MAX_REPORTS_PER_CHAT = 1
MAX_LOOP_TURNS = 8
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def build_tools(report_price_usd: float) -> list[dict[str, Any]]:
    return [
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
            "name": "deep_research_report",
            "description": (
                f"Premium tool: runs several web searches and writes a structured, sourced research "
                f"report (overview, key developments, numbers, risks, outlook). Costs a flat "
                f"${report_price_usd:.2f}, charged by card before the report is written and deducted from the "
                "wallet balance like any other paid tool. Only use it when "
                "the user explicitly asks for a report, deep research, or the premium tool. If the "
                "report can't be produced after payment, the card charge is refunded."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"topic": {"type": "string", "description": "What the report should cover"}},
                "required": ["topic"],
            },
        },
        {
            "name": "get_quote",
            "description": (
                "Free. Current price of a demo stock token (NVDA, AAPL, TSLA, GOOGL, MSFT, SPY), read from the "
                "real Chainlink feed for the matching Robinhood Stock Token on Robinhood Chain mainnet."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"symbol": {"type": "string", "description": "Ticker, e.g. NVDA"}},
                "required": ["symbol"],
            },
        },
        {
            "name": "get_portfolio",
            "description": "Free. The agent's demo portfolio: positions, value, cost basis, P&L, cash, and the trading rules.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "propose_trade",
            "description": (
                "Propose a demo trade (play-money stock tokens on Robinhood Chain testnet at real prices). "
                "This does NOT execute anything: it creates a proposal card the user must approve or reject in "
                "the app. The proposal is checked against the trading rules (kill switch, per-trade cap, daily "
                "limit, allowed tickers). Base it on research you did in this conversation and cite sources."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "NVDA, AAPL, TSLA, GOOGL, MSFT or SPY"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "amount_usd": {"type": "number", "description": "Dollar amount to buy or sell (min $1)"},
                    "thesis": {"type": "string", "description": "2-4 sentences: why, based on the research"},
                    "sources": {"type": "array", "items": {"type": "string"}, "description": "Source URLs"},
                },
                "required": ["symbol", "side", "amount_usd", "thesis"],
            },
        },
        {
            "name": "get_wallet_balance",
            "description": "Return the agent wallet's current balance in USD. Free.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


SYSTEM_PROMPT = """You are AutoWallet, an AI agent that pays for its own tools from a wallet.

search_web is paid per call ($0.001-$0.010 depending on query complexity), and the wallet is charged before results come back. Use it when the user's request needs fresh or external information; answer directly when it doesn't. Prefer one well-formed query over several narrow ones. You can make at most 3 searches per request.

deep_research_report is a premium tool with a flat price, settled by card and deducted from the wallet balance. Use it only when the user asks for a report, deep research, or the premium tool, at most once per request. When it returns a report, present the report to the user (you may tighten it, but keep its sections and facts).

Demo trading: you can research stocks and propose trades in play-money "demo stock tokens" (dNVDA, dAAPL, ...) on Robinhood Chain testnet, priced from real Chainlink feeds. Use get_quote for prices and get_portfolio for holdings. propose_trade only creates a proposal: the user approves or rejects it in the app, and nothing executes until they do. Never say a trade was executed; say it's awaiting their approval. If a proposal is blocked by the trading rules, explain which rule. Make clear this is a demo with play money, not investment advice.

If a paid tool returns an error, tell the user plainly what failed (payment, or the work after payment, and whether it was refunded) and don't invent results. Mention roughly what each paid tool cost."""


@dataclass
class AgentResult:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    search_results: list[dict[str, Any]] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)
    trade_proposals: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        settings: Settings,
        router: PaymentRouter,
        client: Any,
        searcher: Searcher = mock_search,
        reporter: Reporter = mock_report,
        trading: Any = None,
    ):
        self._s = settings
        self._router = router
        self._client = client
        self._search = searcher
        self._report = reporter
        self._trading = trading
        self._tools = build_tools(settings.report_price_usd)

    def _create(self, messages: list[Any]) -> Any:
        params: dict[str, Any] = {
            "model": self._s.claude_model,
            "max_tokens": 16000,
            "system": SYSTEM_PROMPT,
            "tools": self._tools,
            "messages": messages,
            "thinking": {"type": "adaptive"},
        }
        if self._s.claude_fallbacks:
            # Server-side refusal fallback: the API re-runs a declined turn on a fallback model.
            return self._client.beta.messages.create(
                **params, betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"}
            )
        return self._client.messages.create(**params)

    def run(
        self, user_message: str, history: list[dict[str, Any]] | None = None, session_id: str | None = None
    ) -> AgentResult:
        result = AgentResult(text="")
        self._session_id = session_id
        messages: list[Any] = [*(history or []), {"role": "user", "content": user_message}]
        used = {"search_web": 0, "deep_research_report": 0}
        limits = {"search_web": MAX_PAID_SEARCHES_PER_CHAT, "deep_research_report": MAX_REPORTS_PER_CHAT}

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
                if block.name in limits and used[block.name] >= limits[block.name]:
                    content, is_error = (
                        f"Limit of {limits[block.name]} {block.name} call(s) per request reached. "
                        "Answer with what you have.",
                        True,
                    )
                else:
                    if block.name in used:
                        used[block.name] += 1
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

        if name in ("get_quote", "get_portfolio", "propose_trade"):
            return self._trading_tool(name, inputs, result)

        if name == "search_web":
            query = str(inputs.get("query", "")).strip()
            if not query:
                return "query is required", True
            score, amount = price_micros(query)
            return self._paid_call(
                result,
                call={"tool": name, "reason": f"Web search: {query}", "query": query, "score": score},
                amount_micros=amount,
                metadata={"score": score, "query": query},
                deliver=lambda: self._search(query),
                on_delivered=result.search_results.append,
            )

        if name == "deep_research_report":
            topic = str(inputs.get("topic", "")).strip()
            if not topic:
                return "topic is required", True
            return self._paid_call(
                result,
                call={"tool": name, "reason": f"Research report: {topic}", "query": topic},
                amount_micros=usd_to_micros(self._s.report_price_usd),
                metadata={"topic": topic},
                deliver=lambda: self._report(topic),
                on_delivered=result.reports.append,
            )

        return f"Unknown tool: {name}", True

    def _trading_tool(self, name: str, inputs: dict[str, Any], result: AgentResult) -> tuple[str, bool]:
        from backend.trading import PriceError, TradingError

        if self._trading is None:
            return "Demo trading is not configured.", True
        try:
            if name == "get_quote":
                return json.dumps(self._trading.quote(str(inputs.get("symbol", "")))), False
            if name == "get_portfolio":
                return json.dumps(self._trading.portfolio()), False
            research_cost = sum(
                usd_to_micros(c.get("amount_usd", 0)) for c in result.tool_calls if c.get("status") == "paid"
            )
            proposal = self._trading.propose(
                symbol=str(inputs.get("symbol", "")),
                side=str(inputs.get("side", "")),
                amount_usd=float(inputs.get("amount_usd", 0)),
                thesis=str(inputs.get("thesis", "")),
                sources=inputs.get("sources") or [],
                research_cost_micros=research_cost,
                session_id=getattr(self, "_session_id", None),
            )
        except (PriceError, TradingError, ValueError, TypeError) as exc:
            return f"Could not create the proposal: {exc}", True
        result.trade_proposals.append(proposal)
        summary = {k: proposal[k] for k in ("id", "status", "side", "token", "amount_usd", "est_shares", "price_usd", "checks")}
        note = (
            "Proposal created and awaiting the user's approval in the app. It has NOT been executed."
            if proposal["status"] == "pending"
            else "Proposal was BLOCKED by the trading rules; explain which rule failed."
        )
        return json.dumps({"note": note, **summary}), False

    def _paid_call(
        self,
        result: AgentResult,
        *,
        call: dict[str, Any],
        amount_micros: int,
        metadata: dict[str, Any],
        deliver: Callable[[], dict[str, Any]],
        on_delivered: Callable[[dict[str, Any]], None],
    ) -> tuple[str, bool]:
        """Charge first; only then do the work. Refund card charges if the work fails."""
        outcome = self._router.charge(amount_micros, call["reason"], metadata=metadata)
        result.events.extend(outcome.events)
        call.update(amount_usd=micros_to_usd(amount_micros), rail=outcome.rail)
        result.tool_calls.append(call)

        if not outcome.ok:
            call.update(status="failed", error=outcome.error)
            return f"PAYMENT FAILED - nothing was run. {outcome.error}", True

        payment = outcome.payment
        call.update(
            status="paid",
            reference=payment.reference,
            simulated=payment.simulated,
            explorer_url=payment.explorer_url,
            transaction_id=outcome.transaction["id"],
        )

        try:
            delivered = deliver()
        except SearchError as exc:
            refund = self._router.refund(outcome, call["reason"])
            call.update(status="refunded" if refund else "paid_search_failed", error=str(exc))
            if refund:
                call["refund_reference"] = refund["reference"]
                return f"The work failed after payment ({exc}); the card charge was refunded.", True
            return f"The payment went through, but the work itself failed: {exc}", True

        on_delivered(delivered)
        return json.dumps({"payment": self._receipt(outcome), **delivered}), False

    def _receipt(self, outcome: ChargeOutcome) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "charged_usd": micros_to_usd(outcome.amount_micros),
            "rail": outcome.rail,
            "reference": outcome.payment.reference,
            "balance_after_usd": micros_to_usd(self._router.ledger.balance_micros),
        }
        if outcome.events:
            # Tell Claude about refills so it doesn't report a stale balance.
            receipt["auto_topups"] = [{"amount_usd": e["amount_usd"], "status": e["status"]} for e in outcome.events]
        return receipt


def _text(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


def make_client(settings: Settings) -> anthropic.Anthropic | None:
    if not settings.anthropic_api_key:
        return None
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=5, timeout=180.0)
