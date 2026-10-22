from datetime import datetime, date
from decimal import Decimal as D
from zoneinfo import ZoneInfo
import pytest
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import NiftyOpeningRange, current_atm_option

IST = ZoneInfo('Asia/Kolkata')

def test_default_is_index_risk():
    assert NiftyOpeningRange().risk_mode == 'index'

def at(hour, minute):
    return datetime(2026, 10, 20, hour, minute, tzinfo=IST)

def engine(mode='premium'):
    s = NiftyOpeningRange(risk_mode=mode)
    s.set_reference(Bar(at(9, 15), D(100), D(110), D(90), D(100)), observed_at=at(9, 30))
    return s

@pytest.mark.parametrize('hour,price,side', [(10, 111, 'CE'), (12, 89, 'PE')])
def test_delayed_break_and_premium_exits(hour, price, side):
    s = engine()
    assert s.on_tick(at(9, 45), 105) is None
    assert s.on_tick(at(hour, 0), price).action == 'BUY_' + side
    assert s.on_tick(at(hour, 1), price) is None
    s.confirm_entry(index_price=price, option_fill=100)
    assert (s.stop, s.target) == (95, 130)
    assert s.on_tick(at(hour, 2), price, option_price=130).reason == 'target'
    s.confirm_exit()
    assert s.on_tick(at(hour, 3), price) is None

def test_fixed_reference_and_strict_breach():
    s = engine('index')
    assert s.on_tick(at(10, 0), 110) is None
    assert s.on_tick(at(11, 0), 90) is None
    assert s.on_tick(at(12, 0), 111).action == 'BUY_CE'
    s.confirm_entry(index_price=112, option_fill=100)
    assert (s.stop, s.target) == (107, 142)
    assert s.on_tick(at(12, 1), 107).reason == 'stop'

def test_reference_stop_and_end_of_day():
    s = engine('opposite_range')
    s.on_tick(at(10, 0), 89)
    s.confirm_entry(index_price=89, option_fill=100)
    assert (s.stop, s.target) == (115, 59)
    assert s.on_tick(at(15, 15), 100).reason == 'session_end'

def test_incomplete_reference_rejected():
    s = NiftyOpeningRange(risk_mode='index')
    with pytest.raises(ValueError):
        s.set_reference(Bar(at(9, 15), D(100), D(110), D(90), D(100)), observed_at=at(9, 29))

def test_current_expiry_including_expiry_day():
    items = [dict(name='NIFTY', instrument_type='CE', expiry=expiry, strike=strike)
             for expiry in (date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 27))
             for strike in (25000, 25050)]
    chosen = current_atm_option(items, date(2026, 10, 20), 'CE', 25030)
    assert chosen['expiry'] == date(2026, 10, 20)
    assert chosen['strike'] == 25050


def test_backtest_reuses_summary_and_accounts_for_slippage():
    from backtest.nifty_opening_range import run
    from backtest.nifty_15m_breakout import summarize
    rows = [dict(date=at(9, 15).isoformat(), open=100, high=110, low=90, close=100),
            dict(date=at(9, 30).isoformat(), open=100, high=112, low=99, close=111),
            dict(date=at(9, 45).isoformat(), open=111, high=145, low=110, close=145)]
    trades, entry, signals = run(rows, slippage_points=1)
    assert entry is None
    assert len(signals) == 2
    assert trades[0].entry_price == 112
    assert trades[0].exit_price == 144
    assert trades[0].initial_risk == 5
    assert summarize(trades)['net_points'] == 32


def test_backtest_rejects_missing_opening_candle():
    from backtest.nifty_opening_range import run
    with pytest.raises(ValueError, match='09:15'):
        run([dict(date=at(10, 0).isoformat(), open=100, high=110, low=90, close=100)])


def test_configured_index_distances_for_put():
    s = NiftyOpeningRange(stop_points=7, target_points=40)
    s.set_reference(Bar(at(9, 15), D(100), D(110), D(90), D(100)), observed_at=at(9, 30))
    s.on_tick(at(10, 0), 89)
    s.confirm_entry(index_price=89, option_fill=100)
    assert (s.stop, s.target) == (96, 49)
