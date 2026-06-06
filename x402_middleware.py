"""HTTP 402 payment middleware for /search routes."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("autowallet.x402")


def _extract_query(body: bytes) -> str:
    if not body:
        return ""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    query = data.get("query", "")
    return query if isinstance(query, str) else ""


def _replay_request(request: Request, body: bytes) -> Request:
    """Return a new Request whose body stream can be read again downstream."""

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(request.scope, receive)


class X402PaymentMiddleware(BaseHTTPMiddleware):
    """Run complexity scoring and payment routing before /search handlers."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not request.url.path.startswith("/search"):
            return await call_next(request)

        from complexity_scorer import score_query, score_to_price
        from payment_router import route_payment

        body = await request.body()
        query = _extract_query(body)
        score = score_query(query)
        amount_usd = score_to_price(score)
        result = route_payment(amount_usd, score=score)
        rail = result.get("rail", "")

        logger.info(
            "x402_payment path=%s score=%s amount_usd=%s rail=%s status=%s",
            request.url.path,
            score,
            amount_usd,
            rail,
            result.get("status"),
        )

        if result.get("status") == "failed":
            return JSONResponse(
                status_code=402,
                content={
                    "error": "payment_required",
                    "amount_usd": amount_usd,
                    "rail": rail,
                },
            )

        replayed = _replay_request(request, body)
        response = await call_next(replayed)
        response.headers["X-Payment-Rail"] = rail
        response.headers["X-Payment-Amount"] = str(amount_usd)
        response.headers["X-Complexity-Score"] = str(score)
        return response
