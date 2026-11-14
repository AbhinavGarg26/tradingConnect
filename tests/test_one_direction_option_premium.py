from decimal import Decimal as D

import pytest

from backtest.one_direction_option_premium import Charges, evaluate


CHARGES = Charges(D("20"), D("0.1"), D("0.05"), D("0.0001"),
                  D("0.003"), D("18"), D("1"), D("0.05"))


def row(entry="100", exit_price="110"):
    return {"entry_ask": entry, "exit_bid": exit_price, "quantity": "50",
            "entry_time": "2026-11-13T10:00:00+05:30",
            "exit_time": "2026-11-13T11:00:00+05:30",
            "symbol": "NIFTY26111725000CE", "expiry": "2026-11-17",
            "strike": "25000", "side": "CE"}


def test_uses_ask_for_entry_bid_for_exit_and_all_configured_costs():
    report = evaluate([row()], CHARGES)
    assert report["data_basis"] == "historical_option_bid_ask"
    assert report["details"][0]["gross"] == 495.0
    assert report["details"][0]["costs"] > 40
    assert report["details"][0]["net"] < report["details"][0]["gross"]


def test_rejects_index_proxy_or_incomplete_option_rows():
    incomplete = row(); incomplete.pop("entry_ask")
    with pytest.raises(ValueError, match="index proxies are forbidden"):
        evaluate([incomplete], CHARGES)


def test_promotion_needs_sample_profit_and_profit_factor():
    report = evaluate([row() for _ in range(30)], CHARGES)
    assert report["accepted"] is False  # No loss sample means PF is not defensible.

    mixed = [row() for _ in range(25)] + [row(exit_price="80") for _ in range(5)]
    assert evaluate(mixed, CHARGES)["accepted"] is True
