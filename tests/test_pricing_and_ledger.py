from __future__ import annotations

import pytest

from backend.ledger import InsufficientFunds, Ledger, micros_to_usd, usd_to_micros
from backend.pricing import price_micros, score_query, score_to_price


def test_score_hi_is_1():
    assert score_query("hi") == 1


def test_score_comparison_at_least_4():
    assert score_query("compare GPT-4 vs Claude for coding") >= 4


def test_score_latest_research_at_least_4():
    assert score_query("what are the latest AI research developments this week?") >= 4


def test_keywords_match_whole_words_only():
    # "canvas" contains "vs" as a substring; v1 wrongly scored it as a comparison.
    assert score_query("canvas") == 1


@pytest.mark.parametrize("score", range(1, 11))
def test_score_to_price(score: int):
    assert score_to_price(score) == round(score * 0.001, 4)


def test_price_micros_is_exact():
    assert price_micros("hi") == (1, 1_000)


def test_usd_micros_round_trip():
    assert usd_to_micros(0.001) == 1_000
    assert usd_to_micros("10") == 10_000_000
    assert micros_to_usd(1_500) == 0.0015


def test_reserve_commit_debits_once():
    ledger = Ledger(usd_to_micros(1))
    hold = ledger.reserve(1_000, "search")
    assert ledger.balance_micros == 999_000
    txn = ledger.commit(hold, rail="robinhood_chain", reference="0xabc")
    assert ledger.balance_micros == 999_000
    assert txn.to_dict()["amount_usd"] == 0.001
    assert ledger.has_reference("0xABC")


def test_release_restores_funds_and_records_nothing():
    ledger = Ledger(5_000)
    hold = ledger.reserve(5_000, "search")
    ledger.release(hold)
    assert ledger.balance_micros == 5_000
    assert ledger.transactions() == []


def test_reserve_insufficient_raises():
    with pytest.raises(InsufficientFunds):
        Ledger(999).reserve(1_000, "search")


def test_credit_is_negative_in_ui_convention():
    ledger = Ledger(0)
    ledger.credit(5_000_000, "top-up", rail="robinhood")
    assert ledger.transactions()[0]["amount_usd"] == -5.0
