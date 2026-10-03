from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Settings  # noqa: E402
from backend.main import build_services, create_app  # noqa: E402


def make_settings(**overrides: Any) -> Settings:
    """Settings isolated from backend/.env: every rail simulated or unconfigured."""
    base = replace(
        Settings.from_env(),
        anthropic_api_key="",
        claude_model="claude-opus-5",
        claude_fallbacks=False,
        initial_balance_usd=10.0,
        ledger_db_path="",
        database_url="",
        spt_issuer_key="",
        demo_mode=True,
        stripe_secret_key="",
        micropayment_rail="robinhood_chain",
        chain_mode="simulated",
        merchant_address="",
        agent_wallet_private_key="",
        treasury_private_key="",
        crypto_mode="simulated",
        demo_exchange_address="",
        demo_stock_tokens={},
        price_source="fixed",
        auto_topup_enabled=True,
        auto_topup_threshold_usd=1.0,
        auto_topup_amount_usd=5.0,
        auto_topup_daily_cap_usd=25.0,
        circle_api_key="",
        circle_entity_secret="",
        circle_wallet_address="",
        circle_source_wallet_id="",
        circle_token_id="",
        spt_signing_secret="test-secret-" + "x" * 32,
        spt_max_amount_usd=1.0,
        search_provider="mock",
        report_price_usd=0.75,
        search_model="",
        brave_search_api_key="",
    )
    return replace(base, **overrides)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def services(settings: Settings):
    return build_services(settings, setup_stripe=False)


@pytest.fixture
def client(services):
    from fastapi.testclient import TestClient

    with TestClient(create_app(services)) as c:
        yield c


# --- fake Anthropic client ----------------------------------------------------


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(name: str, inputs: dict[str, Any], block_id: str = "toolu_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=inputs)


class FakeMessages:
    def __init__(self, responses: list[SimpleNamespace]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        # Snapshot messages: the agent keeps appending to the same list.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


class FakeAnthropic:
    def __init__(self, responses: list[SimpleNamespace]):
        self.messages = FakeMessages(responses)
        self.beta = SimpleNamespace(messages=self.messages)


def response(stop_reason: str, *blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))
