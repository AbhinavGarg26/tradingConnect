from decimal import Decimal
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from kiteconnect.exceptions import TokenException

from backtest.nifty_15m_breakout import completed_trades, main, next_open_fills, run, summarize
from strategies.nifty_15m_breakout import Bar, Decision, Nifty15mBreakout, Nifty15mLiveAdapter
from nifty_15m_live import (atm_option, confirmed_order, connect_live_with_token_retry,
                            live_start_allowed, next_week_expiry)


def make_bar(time, high, low, close):
    return Bar(time, Decimal(str(close)), Decimal(str(high)), Decimal(str(low)), Decimal(str(close)))


def test_ce_and_pe_with_index_stops_and_targets():
    strategy = Nifty15mBreakout(max_risk_points=None)
    assert strategy.on_bar(make_bar(1, 101, 99, 100)).action == "HOLD"
    buy = strategy.on_bar(make_bar(2, 104, 100, 103))
    assert (buy.action, buy.stop, buy.target) == ("BUY_CE", Decimal(94), Decimal(133))
    assert (buy.reference_high, buy.reference_low) == (Decimal(101), Decimal(99))
    assert strategy.on_bar(make_bar(3, 135, 102, 133)).action == "EXIT_CE"
    sell = strategy.on_bar(make_bar(4, 131, 95, 96))
    assert (sell.action, sell.stop, sell.target) == ("BUY_PE", Decimal(140), Decimal(66))
    assert (sell.prior_entry_high, sell.prior_entry_low) == (Decimal(104), Decimal(100))
    assert strategy.on_bar(make_bar(5, 141, 95, 140)).reason == "stop"


def test_backtest_and_live_use_identical_decisions():
    rows = [
        {"date": str(i), "open": str(close), "high": str(high), "low": str(low), "close": str(close)}
        for i, high, low, close in [(1, 101, 99, 100), (2, 104, 100, 103), (3, 135, 102, 133)]
    ]
    expected = [(d.action, d.stop, d.target) for d in run(rows)]
    emitted = []
    adapter = Nifty15mLiveAdapter(42, emitted.append)
    for row in rows:
        candle = SimpleNamespace(instrument_token=42, timeframe_minutes=15,
                                 open=Decimal(row["open"]), high=Decimal(row["high"]),
                                 low=Decimal(row["low"]), close=Decimal(row["close"]),
                                 open_time=row["date"], is_closed=True)
        adapter.on_candle_close(candle)
    assert [(d.action, d.stop, d.target) for d in emitted] == expected


def test_duplicate_candle_rejected():
    strategy = Nifty15mBreakout()
    bar = make_bar(1, 101, 99, 100)
    strategy.on_bar(bar)
    with pytest.raises(ValueError):
        strategy.on_bar(bar)


def test_next_week_expiry_and_one_atm_contract():
    contracts = [
        {"name": "NIFTY", "instrument_type": side, "expiry": expiry,
         "strike": strike, "lot_size": 65, "tradingsymbol": f"NIFTY{expiry}{strike}{side}"}
        for expiry in (date(2026, 10, 15), date(2026, 10, 22))
        for strike in (25100, 25150)
        for side in ("CE", "PE")
    ]
    expiry = next_week_expiry(contracts, date(2026, 10, 14))
    assert expiry == date(2026, 10, 22)
    option = atm_option(contracts, expiry, "CE", Decimal("25140"))
    assert (option["strike"], option["lot_size"]) == (25150, 65)


def test_live_order_records_one_lot_only_after_confirmed_fill(monkeypatch):
    placed = []
    monkeypatch.setattr("nifty_15m_live.place_protected_market_order",
                        lambda _kite, **kwargs: placed.append(kwargs) or "order-1")
    kite = SimpleNamespace(
        VARIETY_REGULAR="regular", PRODUCT_MIS="MIS", ORDER_TYPE_MARKET="MARKET",
        VALIDITY_DAY="DAY", TRANSACTION_TYPE_BUY="BUY",
        order_history=lambda _order_id: [{"status": "COMPLETE", "average_price": 115.25,
                                          "filled_quantity": 65}],
    )
    submitted = []
    order_id, premium = confirmed_order(
        kite, {"tradingsymbol": "NIFTY-TEST-CE"}, "BUY", 65,
        on_placed=submitted.append)
    assert (order_id, premium, submitted) == ("order-1", Decimal("115.25"), ["order-1"])
    assert placed[0]["quantity"] == 65


def test_live_order_rejects_missing_fill_price(monkeypatch):
    monkeypatch.setattr("nifty_15m_live.place_protected_market_order",
                        lambda _kite, **_kwargs: "order-2")
    kite = SimpleNamespace(
        VARIETY_REGULAR="regular", PRODUCT_MIS="MIS", ORDER_TYPE_MARKET="MARKET",
        VALIDITY_DAY="DAY", order_history=lambda _order_id: [
            {"status": "COMPLETE", "average_price": 0, "filled_quantity": 65}],
    )
    with pytest.raises(RuntimeError, match="average fill price"):
        confirmed_order(kite, {"tradingsymbol": "NIFTY-TEST-CE"}, "BUY", 65)


def test_live_token_retry_accepts_refreshed_session():
    attempts, waits, retries = [], [], []

    def fetch(_logger):
        attempts.append(1)
        if len(attempts) < 3:
            raise SystemExit(1)
        return SimpleNamespace(profile=lambda: {"user_id": "test"}), 42

    kite, user_id = connect_live_with_token_retry(
        fetch, attempts=5, retry_seconds=300,
        now_fn=lambda: datetime(2026, 10, 19, 9, 25), sleep_fn=waits.append,
        on_retry=lambda attempt, total, seconds: retries.append((attempt, total, seconds)))
    assert (user_id, len(attempts), waits) == (42, 3, [300, 300])
    assert retries == [(1, 5, 300), (2, 5, 300)]
    assert kite.profile()["user_id"] == "test"


def test_live_token_retry_checks_broker_and_stops_after_five_attempts():
    attempts, waits = [], []

    def fetch(_logger):
        attempts.append(1)
        return SimpleNamespace(profile=lambda: (_ for _ in ()).throw(TokenException("invalid token"))), 42

    with pytest.raises(RuntimeError, match="after 5 attempts"):
        connect_live_with_token_retry(
            fetch, attempts=5, retry_seconds=300,
            now_fn=lambda: datetime(2026, 10, 19, 9, 15), sleep_fn=waits.append)
    assert (len(attempts), waits) == (5, [300] * 4)


def test_live_retry_refuses_late_or_weekend_start():
    assert live_start_allowed(datetime(2026, 10, 19, 9, 35))
    assert not live_start_allowed(datetime(2026, 10, 19, 9, 45))
    assert not live_start_allowed(datetime(2026, 10, 17, 9, 15))
    with pytest.raises(RuntimeError, match="safe 09:15-09:45"):
        connect_live_with_token_retry(
            lambda _logger: pytest.fail("Late start must not access Kite"),
            now_fn=lambda: datetime(2026, 10, 19, 9, 50),
            sleep_fn=lambda _seconds: pytest.fail("Late start must not retry"))


def test_journal_uses_candle_close_timestamp_and_v3_identity(monkeypatch):
    monkeypatch.setenv("DB_ENCRYPTION_KEY", "test-only-key")
    from trading.strategy_records import STRATEGY_KEY, STRATEGY_VERSION, signal_time

    decision = Decision("BUY_CE", datetime(2026, 9, 4, 11, 15), Decimal("23982.15"))
    assert signal_time(decision) == datetime(2026, 9, 4, 11, 30)
    assert (STRATEGY_KEY, STRATEGY_VERSION) == ("nifty_15m_breakout", "v3")


def test_journal_supplies_rails_required_timestamps(monkeypatch):
    monkeypatch.setenv("DB_ENCRYPTION_KEY", "test-only-key")
    from trading.strategy_records import StrategyEvent, StrategyRun, StrategyTrade

    for model in (StrategyRun, StrategyTrade, StrategyEvent):
        for name in ("created_at", "updated_at"):
            column = model.__table__.c[name]
            assert column.default is not None


def test_session_end_closes_position_and_new_day_resets_reference():
    strategy = Nifty15mBreakout()
    strategy.on_bar(make_bar(datetime(2026, 10, 14, 14, 30), 101, 99, 100))
    assert strategy.on_bar(make_bar(datetime(2026, 10, 14, 14, 45), 104, 100, 103)).action == "BUY_CE"
    ending = strategy.on_bar(make_bar(datetime(2026, 10, 14, 15, 0), 104, 101, 102))
    assert (ending.action, ending.reason) == ("EXIT_CE", "session_end")
    assert strategy.on_bar(make_bar(datetime(2026, 10, 15, 9, 15), 120, 110, 119)).action == "HOLD"


def test_summary_uses_realized_exit_points_and_handles_no_trades():
    signals = [
        Decision("BUY_CE", 1, Decimal(100), Decimal(90), Decimal(130)),
        Decision("EXIT_CE", 2, Decimal(132), Decimal(90), Decimal(130), "target"),
        Decision("BUY_PE", 3, Decimal(100), Decimal(120), Decimal(70)),
        Decision("EXIT_PE", 4, Decimal(125), Decimal(120), Decimal(70), "stop"),
    ]
    trades, open_entry = completed_trades(signals)
    stats = summarize(trades)
    assert open_entry is None
    assert (stats["total"], stats["wins"], stats["losses"]) == (2, 1, 1)
    assert (stats["gross_profit"], stats["gross_loss"], stats["net_points"]) == (
        Decimal(32), Decimal(25), Decimal(7))
    assert stats["expectancy"] == Decimal("3.5")
    assert stats["max_drawdown"] == Decimal(25)
    assert summarize([])["profit_factor"] is None


def test_risk_cap_skips_wide_reference_candle():
    strategy = Nifty15mBreakout()
    strategy.on_bar(make_bar(1, 110, 90, 100))
    decision = strategy.on_bar(make_bar(2, 120, 111, 120))
    assert (decision.action, decision.reason, strategy.position) == ("HOLD", "risk_cap", None)
    emitted = []
    live = Nifty15mLiveAdapter(42, emitted.append, max_risk_points=30)
    for index, high, low, close in [(1, 110, 90, 100), (2, 120, 111, 120)]:
        live.on_candle_close(SimpleNamespace(
            instrument_token=42, timeframe_minutes=15, open=Decimal(close),
            high=Decimal(high), low=Decimal(low), close=Decimal(close),
            open_time=index, is_closed=True))
    assert emitted == []
    rows = [
        {"date": "1", "open": "100", "high": "110", "low": "90", "close": "100"},
        {"date": "2", "open": "120", "high": "120", "low": "111", "close": "120"},
    ]
    assert run(rows) == []
    assert [decision.action for decision in run(rows, max_risk_points=None)] == ["BUY_CE"]


def test_reentry_requires_break_of_previous_entry_candle_range():
    strategy = Nifty15mBreakout()
    strategy.on_bar(make_bar(1, 100, 95, 98))
    assert strategy.on_bar(make_bar(2, 110, 99, 105)).action == "BUY_CE"
    assert strategy.on_bar(make_bar(3, 108, 87, 89)).action == "EXIT_CE"
    # This breaks the immediately prior bar, but not the first entry bar's high.
    assert strategy.on_bar(make_bar(4, 109, 98, 109)).action == "HOLD"
    assert strategy.on_bar(make_bar(5, 112, 105, 111)).action == "BUY_CE"
    assert strategy.entry_count == 2


def test_second_entry_trails_stop_in_ten_point_closed_bar_steps():
    strategy = Nifty15mBreakout()
    strategy.on_bar(make_bar(1, 100, 95, 98))
    strategy.on_bar(make_bar(2, 110, 99, 105))
    strategy.on_bar(make_bar(3, 108, 87, 89))  # first CE exits
    second = strategy.on_bar(make_bar(4, 112, 98, 111))
    assert (second.action, second.stop) == ("BUY_CE", Decimal(82))
    trailed = strategy.on_bar(make_bar(5, 122, 110, 121))
    assert (trailed.action, trailed.reason, trailed.stop) == (
        "HOLD", "trail_stop", Decimal(121))
    assert strategy.stop == Decimal(121)
    assert strategy.on_bar(make_bar(6, 130, 120, 129)).action == "HOLD"
    assert strategy.stop == Decimal(121)
    assert strategy.on_bar(make_bar(7, 130, 118, 119)).reason == "stop"


def test_second_pe_entry_trails_down_and_new_day_resets_reentry_gate():
    strategy = Nifty15mBreakout()
    day = lambda minute: datetime(2026, 9, 4, 9, minute)
    strategy.on_bar(make_bar(day(15), 105, 100, 103))
    strategy.on_bar(make_bar(day(30), 103, 90, 98))  # first PE
    strategy.on_bar(make_bar(day(45), 111, 95, 110))  # stop
    strategy.on_bar(make_bar(datetime(2026, 9, 4, 10, 0), 112, 85, 89))  # second PE
    assert strategy.position == "PE"
    assert strategy.on_bar(make_bar(datetime(2026, 9, 4, 10, 15), 90, 79, 79)).action == "HOLD"
    assert strategy.stop == Decimal(79)
    assert strategy.on_bar(make_bar(datetime(2026, 9, 4, 10, 30), 82, 75, 80)).reason == "stop"
    strategy.on_bar(make_bar(datetime(2026, 9, 5, 9, 15), 200, 195, 198))
    assert strategy.entry_count == 0
    assert strategy.last_entry_bar is None
    assert strategy.on_bar(make_bar(datetime(2026, 9, 5, 9, 30), 210, 198, 205)).action == "BUY_CE"


def test_max_four_entries_per_day_and_entry_reasons():
    strategy = Nifty15mBreakout(max_risk_points=None)
    strategy.on_bar(make_bar(1, 101, 99, 100))
    for count, entry_close in enumerate((104, 136, 168, 200)):
        entry = strategy.on_bar(make_bar(2 + count * 2, entry_close + 1, entry_close - 4, entry_close))
        assert entry.reason == ("first_break_above_previous_high" if count == 0
                                else "fresh_break_above_prior_entry_high")
        strategy.on_bar(make_bar(3 + count * 2, entry_close + 31, entry_close, entry_close + 30))
    assert strategy.entry_count == 4
    decision = strategy.on_bar(make_bar(10, 160, 140, 155))
    assert (decision.action, decision.reason) == ("HOLD", "trade_limit")


def test_next_open_fill_and_slippage_change_realized_result():
    rows = [
        {"date": "2026-09-01 09:15:00", "open": "100", "high": "101", "low": "99", "close": "100"},
        {"date": "2026-09-01 09:30:00", "open": "100", "high": "105", "low": "100", "close": "103"},
        {"date": "2026-09-01 09:45:00", "open": "106", "high": "136", "low": "105", "close": "135"},
        {"date": "2026-09-01 10:00:00", "open": "132", "high": "133", "low": "130", "close": "131"},
    ]
    decisions = run(rows)
    assert [d.action for d in decisions] == ["BUY_CE", "EXIT_CE"]
    trades, open_entry = completed_trades(decisions, next_open_fills(rows), slippage_points=1)
    assert open_entry is None
    assert (trades[0].entry_price, trades[0].exit_price, trades[0].points) == (
        Decimal(107), Decimal(131), Decimal(24))
    with pytest.raises(ValueError, match="Next-candle open missing"):
        completed_trades(decisions, next_open_fills(rows[:-1]))


def test_cli_prints_summary_and_cap_comparison(tmp_path, monkeypatch, capsys):
    path = tmp_path / "nifty_15m.csv"
    path.write_text(
        "date,open,high,low,close\n"
        "2026-09-01 09:15:00,100,101,99,100\n"
        "2026-09-01 09:30:00,100,105,100,103\n"
        "2026-09-01 09:45:00,106,136,105,135\n"
        "2026-09-01 10:00:00,132,133,130,131\n"
    )
    monkeypatch.setattr("sys.argv", ["backtest", str(path), "--summary-only",
                                 "--max-risk-points", "30", "--slippage-points", "1"])
    main()
    output = capsys.readouterr().out
    assert "Completed trades: 1" in output
    assert "Net: 24.00" in output
    assert "CE trades: 1" in output
    assert "Gross profit +24.00" in output
    assert "FINAL P&L (NIFTY INDEX POINTS, NOT ₹): +24.00" in output
    assert output.rstrip().endswith("=" * 56)
    assert "Max closed-trade drawdown" in output
    assert "SAME-DATA COMPARISON" in output
    assert "time,action" not in output
