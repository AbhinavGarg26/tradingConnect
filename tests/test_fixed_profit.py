from datetime import datetime, timedelta
from decimal import Decimal as D
import pytest

from strategies.fixed_profit import FixedProfit
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST
from backtest.fixed_profit import run_session, statistics

START = datetime(2026, 10, 23, 9, 15, tzinfo=IST)


def bar(minute, o, h, l, c):
    return Bar(START + timedelta(minutes=minute), *(D(str(p)) for p in (o, h, l, c)))


def engine(**kwargs):
    config = dict(min_range_points=20, confirmation_minutes=1, tolerance_points=2,
                  setup_mode='retest', follow_through=True, stop_points=10)
    config.update(kwargs)
    e = FixedProfit(**config)
    e.set_reference(bar(0, 110, 130, 100, 120), observed_at=START + timedelta(minutes=30))
    return e


def feed(e, b):
    return e.on_bar(b, observed_at=b.time + timedelta(minutes=e.confirmation_minutes))


@pytest.mark.parametrize('side', [1, -1])
def test_break_retest_follow_through_and_fixed_exits(side):
    e = engine()
    # Mirror prices around 115 for a symmetric downward setup.
    def b(m, o, h, l, c):
        return bar(m, o, h, l, c) if side == 1 else bar(m, 230-o, 230-l, 230-h, 230-c)
    assert feed(e, b(30, 129, 136, 128, 134)) is None
    assert feed(e, b(31, 132, 134, 129, 133)) is None
    signal = feed(e, b(32, 133, 137, 132, 136))
    assert signal.action == ('BUY_CE' if side == 1 else 'BUY_PE')
    fill = 136 if side == 1 else 94
    e.confirm_entry(index_price=fill)
    assert e.stop == fill - side * 10
    assert e.target == fill + side * 30
    decision = e.on_tick(START + timedelta(minutes=34), e.target)
    assert decision.reason == 'target'
    e.confirm_exit()
    assert feed(e, b(35, 129, 136, 128, 134)) is None


def test_breakout_cannot_retest_itself():
    e = engine(follow_through=False)
    assert feed(e, bar(30, 129, 136, 129, 134)) is None
    assert feed(e, bar(31, 132, 134, 129, 133)).action == 'BUY_CE'


def test_failed_retest_and_gap_reset():
    e = engine()
    feed(e, bar(30, 129, 136, 128, 134))
    feed(e, bar(31, 132, 133, 127, 129))
    assert e.breakout is None
    feed(e, bar(32, 129, 136, 128, 134))
    # Missing minute resets the setup: this candle is a new breakout only.
    assert feed(e, bar(34, 132, 136, 129, 134)) is None
    assert e.rejection is None


def test_original_support_rejection():
    e = engine(setup_mode='rejection')
    assert feed(e, bar(30, 102, 105, 99, 104)) is None
    assert feed(e, bar(31, 104, 108, 103, 107)).action == 'BUY_CE'
    with pytest.raises(ValueError, match='chase'):
        e.confirm_entry(index_price=112)
    e.reject_signal()
    assert e.pending is None


def test_stop_must_be_beyond_wick():
    e = engine(follow_through=False)
    feed(e, bar(30, 129, 136, 128, 134))
    # Entry 139 would put stop 129 above the low at 128.
    assert feed(e, bar(31, 132, 140, 128, 139)) is None
    assert e.pending is None


def test_closed_chronological_session_bars_only():
    e = engine()
    b = bar(30, 129, 136, 128, 134)
    with pytest.raises(ValueError, match='closed'):
        e.on_bar(b, observed_at=b.time)
    feed(e, b)
    with pytest.raises(ValueError, match='chronological'):
        feed(e, b)
    with pytest.raises(ValueError, match='session'):
        feed(e, bar(30+1440, 129, 136, 128, 134))


def test_no_tick_entry_and_strict_range_filter():
    e = engine()
    assert e.on_tick(START + timedelta(minutes=30), 135) is None
    assert e.pending is None
    e = FixedProfit(min_range_points=30)
    e.set_reference(bar(0, 110, 130, 100, 120), observed_at=START+timedelta(minutes=30))
    assert not e.eligible


def test_backtest_next_open_entry_and_stop_first_when_both_hit():
    bars = [bar(i, 110, 130, 100, 120) for i in range(30)]
    bars += [bar(30, 129, 136, 128, 134), bar(31, 132, 134, 129, 133),
             bar(32, 133, 138, 132, 136), bar(33, 135, 170, 120, 150)]
    result = run_session(bars, dict(min_range_points=20, confirmation_minutes=1,
                                  tolerance_points=2, setup_mode='retest', follow_through=True, stop_points=10))
    assert result['entry'] == 135
    assert result['exit_reason'] == 'stop'
    assert result['points'] == -10
    assert result['entry_time'] == bars[33].time.isoformat()


def test_cost_and_drawdown_statistics():
    result = statistics([{'points': 30}, {'points': -10}, {'points': -10}], cost=2)
    assert result['net_points'] == 4
    assert result['max_drawdown_points'] == 24


@pytest.mark.parametrize('direction', [1, -1])
def test_default_baseline_uses_fifteen_point_stop(direction):
    e = engine(stop_points=15, follow_through=False)
    def b(m, o, h, l, c):
        return bar(m, o, h, l, c) if direction == 1 else bar(m, 230-o, 230-l, 230-h, 230-c)
    feed(e, b(30, 129, 136, 128, 134))
    assert feed(e, b(31, 132, 136, 129, 135))
    fill = 135 if direction == 1 else 95
    e.confirm_entry(index_price=fill)
    assert e.stop == fill - direction * 15
    assert e.target == fill + direction * 30
    assert e.on_tick(START + timedelta(minutes=33), fill-direction*11) is None
    assert e.on_tick(START + timedelta(minutes=34), e.stop).reason == 'stop'
    assert FixedProfit().stop_points == 15


def test_fifteen_point_exit_keeps_original_ten_point_entry_qualification():
    e = engine(stop_points=15)
    assert not e._entry_valid(D(139), (1, D(130), D(124)))
    assert not e._entry_valid(D(139), (1, D(130), D(125)))
    assert e._entry_valid(D(137), (1, D(130), D(128)))
    assert e._entry_valid(D(137), (1, D(130), D(128))) == engine(stop_points=10)._entry_valid(D(137), (1, D(130), D(128)))
    # Chase limit stays 10 even with the wider stop.
    assert not e._entry_valid(D(141), (1, D(130), D(129)))
