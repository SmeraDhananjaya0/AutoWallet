from __future__ import annotations

import base64
import json
from decimal import Decimal

import httpx
import pytest
from nacl.signing import SigningKey

from backend.treasury.robinhood_crypto import RobinhoodCryptoClient, RobinhoodCryptoError, sign_request

SEED = bytes(range(32))
SEED_B64 = base64.b64encode(SEED).decode()


def test_signature_covers_key_timestamp_path_method_body():
    key = SigningKey(SEED)
    sig = sign_request(key, "rh-api-key", 1_700_000_000, "/api/v1/crypto/trading/orders/", "post", '{"a":1}')
    message = b'rh-api-key1700000000/api/v1/crypto/trading/orders/POST{"a":1}'
    key.verify_key.verify(message, base64.b64decode(sig))  # raises if wrong


def test_rejects_malformed_private_key():
    with pytest.raises(RobinhoodCryptoError):
        RobinhoodCryptoClient("key", base64.b64encode(b"short").decode())


def test_buy_usd_amount_signs_and_places_market_order(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/trading_pairs/"):
            return httpx.Response(200, json={"results": [{"asset_increment": "0.01", "min_order_size": "1"}]})
        if path.endswith("/best_bid_ask/"):
            return httpx.Response(200, json={"results": [{"ask_inclusive_of_buy_spread": "1.0005"}]})
        if path.endswith("/orders/") and request.method == "POST":
            return httpx.Response(201, json={"id": "ord_1", "state": "filled"})
        return httpx.Response(404)

    client = RobinhoodCryptoClient("rh-api-key", SEED_B64)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    order = client.buy_usd_amount("USDC-USD", Decimal("5"))
    assert order["state"] == "filled"

    post = seen[-1]
    body = json.loads(post.content)
    assert body["type"] == "market" and body["side"] == "buy" and body["symbol"] == "USDC-USD"
    assert body["market_order_config"]["asset_quantity"] == "4.99"  # 5 / 1.0005 rounded down to 0.01

    # Signature on the wire verifies against the exact path+query and body sent.
    for req in seen:
        path = req.url.raw_path.decode()
        message = f"rh-api-key{req.headers['x-timestamp']}{path}{req.method}{req.content.decode()}"
        SigningKey(SEED).verify_key.verify(message.encode(), base64.b64decode(req.headers["x-signature"]))
