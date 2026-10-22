from datetime import datetime, timedelta
from decimal import Decimal as D
import pytest
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_midpoint_ema import IST, NiftyMidpointEMA
from backtest.nifty_midpoint_ema import run

START = datetime(2026, 10, 20, 9, 15, tzinfo=IST)
def bar(i, close=100, opening=None):
    c = D(str(close)); o = c if opening is None else D(str(opening))
    return Bar(START + timedelta(minutes=i), o, max(o, c), min(o, c), c)

def warm(s, descending=False):
    for i in range(36):
        signal = s.on_bar(bar(i, 100 + (-i if descending else i)))
    return signal

@pytest.mark.parametrize('descending,side', [(False, 'CE'), (True, 'PE')])
def test_entry_and_pending_confirmation(descending, side):
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=5)
    signal = warm(s, descending)
    assert signal.action == 'BUY_' + side
    assert s.position is None
    assert s.on_bar(bar(36, 64 if descending else 136)) is None
    s.confirm_fill()
    assert s.position == side
    assert s.on_bar(bar(37, 63 if descending else 137)) is None


def test_body_uses_open_close_not_wicks_and_no_early_entry():
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=5)
    for i in range(30):
        b = bar(i, 200 if i == 29 else 100)
        if i == 0:
            b = Bar(b.time, D(100), D(1000), D(1), D(100))
        assert s.on_bar(b) is None
    assert (s.upper, s.lower) == (155, 145)


def test_price_failure_and_reentry():
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=0)
    warm(s); s.confirm_fill()
    signal = s.on_bar(bar(36, 110))
    assert signal.action == 'EXIT_CE'
    assert signal.reason == 'price_failed'
    s.confirm_fill()
    assert s.on_bar(bar(37, 140)).action == 'BUY_CE'


def test_ema_failure_alone_exits():
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=5)
    warm(s); s.confirm_fill()
    # Keep the close above the fixed upper level while short EMA decays.
    for i in range(36, 120):
        signal = s.on_bar(bar(i, 116))
        if signal:
            assert signal.action == 'EXIT_CE'
            assert signal.reason == 'ema_failed'
            break
    else:
        pytest.fail('Expected EMA exit')


def test_missing_bar_and_incomplete_day_rejected():
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=5)
    s.on_bar(bar(0))
    with pytest.raises(ValueError, match='Missing'):
        s.on_bar(bar(2))
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=5)
    warm(s); s.confirm_fill()
    with pytest.raises(ValueError, match='Reconcile'):
        s.on_bar(Bar(START + timedelta(days=1), D(100), D(100), D(100), D(100)))


def test_square_off_and_no_new_entry_after_cutoff():
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=5)
    warm(s); s.confirm_fill()
    for i in range(36, 360):
        signal = s.on_bar(bar(i, 135))
    assert signal.reason == 'session_end'
    s.confirm_fill(fill_time=signal.time)
    assert s.on_bar(bar(360, 140)) is None


def test_next_open_fills_and_final_pending():
    bars = [bar(i, 100 + i) for i in range(36)]
    bars += [bar(36, 110, opening=140), bar(37, 110, opening=108)]
    rows = [dict(date=b.time.isoformat(), open=b.open, high=b.high, low=b.low, close=b.close) for b in bars]
    trades, entry, pending, signals = run(rows,early_doji_stop_points=None,pe_doji_body_percent=None, slippage_points=1, logic='gap')
    assert len(trades) == 1
    assert trades[0].entry_price == 141
    assert trades[0].exit_price == 107
    assert trades[0].points == -34
    assert entry is None
    _, entry, pending, _ = run(rows[:36],early_doji_stop_points=None,pe_doji_body_percent=None, logic='gap')
    assert entry is None and pending.action == 'BUY_CE'


def test_no_thirty_point_target():
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=5)
    warm(s); s.confirm_fill()
    assert s.on_bar(bar(36, 200)) is None
    assert s.position == 'CE'


def test_equal_midpoint_is_neither_side():
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=5)
    for i in range(40):
        assert s.on_bar(bar(i, 100)) is None
    assert s.upper == s.lower == 100
    assert s.position is None


def test_ema_recurrence_matches_independent_formula():
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=5)
    expected12 = expected36 = D(100)
    for i in range(40):
        close = D(100 + i)
        if i:
            expected12 = (close * 2 + expected12 * 11) / 13
            expected36 = (close * 2 + expected36 * 35) / 37
        s.on_bar(bar(i, close))
        assert abs(s.ema12 - expected12) < D('1e-23')
        assert abs(s.ema36 - expected36) < D('1e-23')


def test_details_show_fill_pnl_and_rounded_ema(capsys):
    from backtest.nifty_midpoint_ema import Trade, print_details
    from strategies.nifty_midpoint_ema import Signal
    signal = Signal('BUY_PE', START, D('22859.7'), 'conditions_met',
                    D('23148.195'), D('23147.605'),
                    D('22870.02282745783207988996938'), D('22870.29819137959301732970246'))
    trade = Trade('PE', START, START + timedelta(minutes=8), D('22858.70'),
                  D('22874.55'), D('-15.85'), 'session_end')
    print_details([signal], [trade])
    output = capsys.readouterr().out
    assert '22870.02,22870.30,-0.275364' in output
    assert '22858.70,22874.55,-15.85,LOSS,session_end' in output
    assert 'not fill prices' in output


@pytest.mark.parametrize('descending', [False, True])
def test_cooldown_uses_actual_exit_fill_and_allows_entry_at_boundary(descending):
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=5)
    warm(s, descending); s.confirm_fill()
    exit_signal = s.on_bar(bar(36, 90 if not descending else 110))
    assert exit_signal.action.startswith('EXIT_')
    # Signal at 09:52, delayed actual fill at 09:53:30; unlock at 09:58:30.
    fill = exit_signal.time + timedelta(minutes=1, seconds=30)
    s.confirm_fill(fill_time=fill)
    assert s.cooldown_until == fill + timedelta(minutes=5)
    for i in range(37, 43):
        assert s.on_bar(bar(i, 160 if not descending else 40)) is None
    assert s.on_bar(bar(43, 160 if not descending else 40)).action == ('BUY_PE' if descending else 'BUY_CE')


def test_cooldown_blocks_opposite_side_and_rejected_exit_does_not_start_timer():
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=0, cooldown_minutes=5)
    warm(s); s.confirm_fill()
    signal = s.on_bar(bar(36, 1))
    s.reject_signal()
    assert s.position == 'CE' and s.cooldown_until is None
    signal = s.on_bar(bar(37, 1))
    with pytest.raises(ValueError, match='exit fill time'):
        s.confirm_fill()
    assert s.position == 'CE' and s.pending == signal
    s.confirm_fill(fill_time=signal.time)
    for i in range(38, 42):
        assert s.on_bar(bar(i, 1)) is None
    assert s.on_bar(bar(42, 1)).action == 'BUY_PE'


@pytest.mark.parametrize('value', [-1, 1.5, True])
def test_invalid_cooldown(value):
    with pytest.raises(ValueError, match='Cooldown'):
        NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', cooldown_minutes=value)


def force_next_gap(s, i, price, gap):
    # Choose prior EMA state so the next recurrence produces the requested gap.
    p, g = D(str(price)), D(str(gap))
    s.ema36 = p
    s.ema12 = p + g * 13 / 11
    return s.on_bar(bar(i, p))


def test_version_b_defaults_and_variant_labels():
    from strategies.nifty_midpoint_ema import variant_label
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap')
    assert s.ema_gap_points == D('2.5') and s.cooldown_minutes == 0
    assert 'Version B' in variant_label('2.5', 0)
    assert 'Version A' in variant_label(0, 5)
    assert 'Baseline' in variant_label(0, 0)
    assert variant_label(5, 5) == 'custom'


@pytest.mark.parametrize('side,price,direction', [('CE',140,1), ('PE',60,-1)])
def test_version_b_entry_band_hold_opposite_exit_and_no_cooldown(side, price, direction):
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap')
    warm(s); s.reject_signal()
    assert force_next_gap(s,36,price,D('2.4')*direction) is None
    signal = force_next_gap(s,37,price,D('2.6')*direction)
    assert signal.action == 'BUY_' + side
    s.confirm_fill()
    assert force_next_gap(s,38,price,0) is None
    assert force_next_gap(s,39,price,D('-2.4')*direction) is None
    signal = force_next_gap(s,40,price,D('-2.6')*direction)
    assert signal.action == 'EXIT_' + side and signal.reason == 'ema_failed'
    s.confirm_fill()
    assert s.cooldown_until is None
    assert force_next_gap(s,41,price,D('2.6')*direction).action == 'BUY_' + side


def test_version_b_price_exit_remains_active_inside_ema_band():
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap')
    warm(s); s.confirm_fill()
    signal = force_next_gap(s,36,110,0)
    assert signal.action == 'EXIT_CE' and signal.reason == 'price_failed'


@pytest.mark.parametrize('gap', [-1, 'NaN', 'Infinity'])
def test_invalid_ema_gap(gap):
    with pytest.raises(ValueError, match='EMA gap'):
        NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='gap', ema_gap_points=gap)


def cross_engine():
    s = NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='price_cross', candle_stop=False)
    for i in range(36):
        assert s.on_bar(bar(i,100)) is None
    return s


@pytest.mark.parametrize('side,direction', [('CE',1),('PE',-1)])
def test_version_c_fresh_cross_exit_two_consecutive_and_reentry(side,direction):
    s = cross_engine()
    signal = s.on_bar(bar(36,100+10*direction))
    assert signal.action == 'BUY_'+side and signal.reason == 'cross_confirmed_ema36'
    s.confirm_fill()
    # Only a completed candle after entry can arm the exit.
    assert s.on_bar(bar(37,100+15*direction)) is None
    assert s.exit_armed
    assert s.on_bar(bar(38,100-direction)) is None
    assert s.exit_close_count == 1
    assert s.on_bar(bar(39,100+15*direction)) is None
    assert s.exit_close_count == 0
    assert s.on_bar(bar(40,100-direction)) is None
    signal = s.on_bar(bar(41,100-2*direction))
    assert signal.action == 'EXIT_'+side and signal.reason == 'two_closes_against_ema12'
    s.confirm_fill()
    assert s.exit_close_count == 0 and not s.exit_armed
    assert s.on_bar(bar(42,100-3*direction)) is None
    assert s.on_bar(bar(43,100+15*direction)).action == 'BUY_'+side


def test_version_c_filter_can_confirm_after_cross():
    s=NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, logic='price_cross', candle_stop=False)
    for i in range(36):
        assert s.on_bar(bar(i,100,opening=200 if i==0 else None)) is None
    assert (s.upper,s.lower)==(155,145)
    assert s.on_bar(bar(36,110)) is None  # Cross but price filter fails.
    assert s.on_bar(bar(37,170)).action == 'BUY_CE'  # Confirms the active cross.


def test_version_c_ema12_equality_resets_exit_count():
    s=cross_engine()
    s.on_bar(bar(36,110));s.confirm_fill()
    s.on_bar(bar(37,115))
    s.on_bar(bar(38,99))
    assert s.exit_close_count == 1
    assert s.on_bar(bar(39,s.ema12)) is None
    assert s.exit_close_count == 0


def test_version_c_backtest_next_open_and_distinct_label(capsys):
    from backtest.nifty_midpoint_ema import print_summary
    bars=[bar(i,100) for i in range(36)]
    bars += [bar(36,110),bar(37,115,opening=112),bar(38,99),bar(39,98),bar(40,97,opening=96)]
    rows=[dict(date=b.time.isoformat(),open=b.open,high=b.high,low=b.low,close=b.close) for b in bars]
    trades,entry,pending,signals=run(rows,early_doji_stop_points=None,pe_doji_body_percent=None,slippage_points=1,candle_stop=False)
    assert len(trades)==1 and trades[0].points == -18
    assert trades[0].reason == 'two_closes_against_ema12'
    assert entry is None and pending is None
    print_summary(trades,entry,pending)
    assert 'Version C' in capsys.readouterr().out


@pytest.mark.parametrize('direction', [1,-1])
def test_unarmed_exit_ignores_opposite_closes_then_arms(direction):
    s=cross_engine()
    s.on_bar(bar(36,100+10*direction));s.confirm_fill()
    assert not s.exit_armed
    for i in range(37,40):
        assert s.on_bar(bar(i,100-direction)) is None
        assert not s.exit_armed and s.exit_close_count==0
    assert s.on_bar(bar(40,s.ema12)) is None  # Equality cannot arm.
    assert not s.exit_armed
    assert s.on_bar(bar(41,100+15*direction)) is None
    assert s.exit_armed and s.exit_close_count==0
    assert s.on_bar(bar(42,100-direction)) is None
    assert s.on_bar(bar(43,100-2*direction)).action.startswith('EXIT_')


def test_square_off_exits_even_when_not_armed():
    s=cross_engine()
    s.on_bar(bar(36,110));s.confirm_fill()
    for i in range(37,360):
        signal=s.on_bar(bar(i,1))
        if i<359:
            assert signal is None and not s.exit_armed
    assert signal.reason=='session_end' and signal.action=='EXIT_CE'


@pytest.mark.parametrize('side,entry,previous_open,stop', [('PE',90,110,105),('CE',110,90,95)])
def test_previous_body_stop_is_fixed_and_active_before_ema_exit_arms(side,entry,previous_open,stop):
    s=NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None)
    for i in range(36):
        s.on_bar(bar(i,100,opening=previous_open if i==35 else None))
    signal=s.on_bar(bar(36,entry,opening=500))
    assert signal.action=='BUY_'+side and signal.stop==stop
    assert s.stop is None  # No position/stop until confirmed buy.
    s.confirm_fill()
    assert s.stop==stop and not s.exit_armed
    signal=s.on_bar(bar(37,stop))
    assert signal.action=='EXIT_'+side and signal.reason=='candle_midpoint_stop'
    assert signal.stop==stop
    s.confirm_fill()
    assert s.stop is None


def test_stop_recalculated_for_every_fresh_cross_and_not_trailed():
    s=NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None)
    for i in range(36):s.on_bar(bar(i,100))
    signal=s.on_bar(bar(36,90));s.confirm_fill()
    assert s.stop==100
    signal=s.on_bar(bar(37,106,opening=114))
    assert signal.reason=='candle_midpoint_stop'
    s.confirm_fill()
    signal=s.on_bar(bar(38,90))
    assert signal.action=='BUY_PE' and signal.stop==110
    s.confirm_fill()
    assert s.on_bar(bar(39,80)) is None
    assert s.stop==110


def test_candle_stop_backtest_uses_following_open_not_exact_stop():
    bars=[bar(i,100) for i in range(36)]
    bars += [bar(36,90),bar(37,101,opening=89),bar(38,102,opening=104)]
    rows=[dict(date=b.time.isoformat(),open=b.open,high=b.high,low=b.low,close=b.close) for b in bars]
    trades,entry,pending,signals=run(rows,early_doji_stop_points=None,pe_doji_body_percent=None,slippage_points=1)
    assert len(trades)==1 and trades[0].reason=='candle_midpoint_stop'
    assert trades[0].entry_price==88 and trades[0].exit_price==105
    assert trades[0].points==-17
    assert signals[0].stop==100


@pytest.mark.parametrize('direction', [1,-1])
def test_shallow_cross_confirms_on_later_candle(direction):
    s=NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None)
    for i in range(36):s.on_bar(bar(i,100,opening=100+4*direction if i==35 else None))
    assert s.on_bar(bar(36,100+2*direction)) is None
    assert s.cross_side == ('CE' if direction==1 else 'PE')
    signal=s.on_bar(bar(37,100+10*direction))
    assert signal.action==('BUY_CE' if direction==1 else 'BUY_PE')
    # Stop stays anchored to the candle before the original crossing.
    assert signal.stop==100+2*direction
    s.confirm_fill()
    assert s.cross_side is None


@pytest.mark.parametrize('direction', [1,-1])
def test_cross_distance_accepts_exact_three(direction):
    s=NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None)
    for i in range(36):s.on_bar(bar(i,100))
    close=D(100)+D(direction)*D(111)/35
    signal=s.on_bar(bar(36,close))
    assert abs(close-s.ema36)==3
    assert signal.action==('BUY_CE' if direction==1 else 'BUY_PE')


@pytest.mark.parametrize('distance', [-1,'NaN','Infinity'])
def test_invalid_cross_distance(distance):
    with pytest.raises(ValueError,match='Entry distance'):
        NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, entry_distance_points=distance)


@pytest.mark.parametrize('direction', [1,-1])
def test_pending_cross_cancels_at_ema_and_does_not_retain_stop(direction):
    s=NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None)
    for i in range(36):s.on_bar(bar(i,100))
    assert s.on_bar(bar(36,100+2*direction)) is None
    assert s.cross_side is not None
    assert s.on_bar(bar(37,s.ema36)) is None
    assert s.cross_side is None and s.cross_stop is None
    assert s.on_bar(bar(38,s.ema36)) is None


@pytest.mark.parametrize('direction',[1,-1])
def test_seven_entry_cap_keeps_exits_active_and_resets_next_day(direction):
    s=NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None)
    for i in range(36):s.on_bar(bar(i,100))
    for trade in range(7):
        signal=s.on_bar(bar(36+2*trade,100+10*direction))
        assert signal.action.startswith('BUY_')
        assert s.entry_count==trade  # Counts fills, not signals.
        s.confirm_fill()
        signal=s.on_bar(bar(37+2*trade,100-10*direction))
        assert signal.action.startswith('EXIT_')
        s.confirm_fill()
    assert s.entry_count==7 and s.position is None
    for i in range(50,56):
        assert s.on_bar(bar(i,110 if i%2==0 else 90)) is None
    for i in range(36):
        b=bar(i,100)
        s.on_bar(Bar(b.time+timedelta(days=1),b.open,b.high,b.low,b.close))
    assert s.entry_count==0
    b=bar(36,110)
    assert s.on_bar(Bar(b.time+timedelta(days=1),b.open,b.high,b.low,b.close)).action=='BUY_CE'


def test_rejected_entry_does_not_consume_limit():
    s=NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, max_entries=1)
    for i in range(36):s.on_bar(bar(i,100))
    s.on_bar(bar(36,110));s.reject_signal()
    assert s.entry_count==0
    assert s.on_bar(bar(37,90)).action=='BUY_PE'
    s.confirm_fill()
    assert s.entry_count==1


@pytest.mark.parametrize('limit',[0,-1,True,1.5])
def test_invalid_daily_limit(limit):
    with pytest.raises(ValueError,match='Maximum entries'):
        NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=None, max_entries=limit)


def test_pe_doji_waits_for_later_non_doji_and_preserves_cross_stop():
    s=NiftyMidpointEMA(early_doji_stop_points=None)
    for i in range(36):s.on_bar(bar(i,100))
    # Fresh qualifying cross, but body is only 10% of the high/low range.
    t=bar(36).time
    assert s.on_bar(Bar(t,D(91),D(95),D(85),D(90))) is None
    assert s.cross_side=='PE' and s.cross_stop==100 and s.entry_count==0
    # Another doji while still well below EMA36 must also be skipped.
    assert s.on_bar(Bar(bar(37).time,D(89),D(94),D(84),D(89))) is None
    signal=s.on_bar(Bar(bar(38).time,D(90),D(91),D(87),D(88)))
    assert signal.action=='BUY_PE' and signal.stop==100
    s.confirm_fill()
    assert s.entry_count==1


def test_zero_range_pe_is_doji_but_ce_filter_is_unchanged():
    for direction in (-1,1):
        s=NiftyMidpointEMA(early_doji_stop_points=None)
        for i in range(36):s.on_bar(bar(i,100))
        signal=s.on_bar(bar(36,100+10*direction))
        if direction<0:
            assert signal is None and s.cross_side=='PE'
        else:
            assert signal.action=='BUY_CE'


def test_pe_doji_setup_cancels_if_price_returns_above_ema36():
    s=NiftyMidpointEMA(early_doji_stop_points=None)
    for i in range(36):s.on_bar(bar(i,100,opening=200 if i==0 else None))
    assert s.on_bar(bar(36,90)) is None
    assert s.cross_side=='PE'
    assert s.on_bar(bar(37,110)) is None  # CE fails opening body threshold.
    assert s.cross_side=='CE'


@pytest.mark.parametrize('percent',[-1,101,'NaN'])
def test_invalid_doji_percent(percent):
    with pytest.raises(ValueError,match='PE doji'):
        NiftyMidpointEMA(early_doji_stop_points=None, pe_doji_body_percent=percent)


def early_stop_engine(side='PE'):
    s=NiftyMidpointEMA(early_doji_stop_points=5)
    for i in range(36):s.on_bar(bar(i,100,opening=(140 if side=='PE' else 60) if i==35 else None))
    price=90 if side=='PE' else 110
    signal=s.on_bar(bar(36,price,opening=100))
    assert signal.action=='BUY_'+side
    s.confirm_fill(fill_time=bar(37).time,index_price=89 if side=='PE' else 111)
    return s


@pytest.mark.parametrize('side,close,expected',[('PE',80,84),('CE',120,116)])
def test_early_doji_locks_five_points_from_actual_fill(side,close,expected):
    s=early_stop_engine(side)
    assert s.on_bar(Bar(bar(37).time,D(close),D(close+5),D(close-5),D(close))) is None
    assert s.stop==expected and s.doji_stop_tightened
    assert s.post_entry_candles==1
    signal=s.on_bar(bar(38,expected))
    assert signal.action=='EXIT_'+side and signal.reason=='early_doji_stop'
    s.confirm_fill()
    assert s.entry_index_price is None and s.post_entry_candles==0


def test_early_doji_never_widens_existing_stop():
    s=early_stop_engine()
    s.stop=D(82)
    s.on_bar(Bar(bar(37).time,D(80),D(85),D(75),D(80)))
    assert s.stop==82 and not s.doji_stop_tightened


def test_fourth_doji_does_not_tighten_stop():
    s=early_stop_engine()
    for i,price in [(37,85),(38,82),(39,79)]:
        assert s.on_bar(bar(i,price,opening=price+2)) is None
    assert s.on_bar(Bar(bar(40).time,D(79),D(84),D(74),D(79))) is None
    assert s.post_entry_candles==4 and s.stop==120 and not s.doji_stop_tightened


def test_third_candle_doji_can_trigger_exit_at_same_close():
    s=early_stop_engine()
    assert s.on_bar(bar(37,85,opening=87)) is None
    assert s.on_bar(bar(38,82,opening=84)) is None
    signal=s.on_bar(Bar(bar(39).time,D(96),D(101),D(91),D(96)))
    assert s.post_entry_candles==3 and s.stop==84
    assert signal.reason=='early_doji_stop'  # Already beyond tightened stop at observation.


@pytest.mark.parametrize('points',[0,-1,'NaN'])
def test_invalid_early_doji_buffer(points):
    with pytest.raises(ValueError,match='Early doji'):
        NiftyMidpointEMA(early_doji_stop_points=points)


@pytest.mark.parametrize('side,close,expected',[('PE',90,84),('CE',110,116),('PE',84,84),('CE',116,116)])
def test_early_doji_exits_when_profit_stop_not_yet_achieved_or_equal(side,close,expected):
    s=early_stop_engine(side)
    signal=s.on_bar(Bar(bar(37).time,D(close),D(close+5),D(close-5),D(close)))
    assert signal.action=='EXIT_'+side
    assert signal.reason=='early_doji_stop' and signal.stop==expected
    assert s.position==side  # Signal does not pretend the exit filled.


def test_early_doji_exit_backtest_still_fills_next_open():
    bars=[bar(i,100,opening=140 if i==35 else None) for i in range(36)]
    bars += [bar(36,90,opening=100),Bar(bar(37).time,D(90),D(95),D(85),D(90)),bar(38,91,opening=92)]
    rows=[dict(date=b.time.isoformat(),open=b.open,high=b.high,low=b.low,close=b.close) for b in bars]
    trades,entry,pending,signals=run(rows,slippage_points=1,early_doji_stop_points=5)
    assert signals[-1].reason=='early_doji_stop'
    assert trades[0].exit_time==bar(38).time and trades[0].exit_price==93
    assert trades[0].points==-4


def test_default_has_no_early_doji_exit_or_stop_adjustment():
    s=NiftyMidpointEMA()
    assert s.early_doji_stop_points is None
    for i in range(36):s.on_bar(bar(i,100,opening=140 if i==35 else None))
    s.on_bar(bar(36,90,opening=100))
    s.confirm_fill(fill_time=bar(37).time,index_price=89)
    assert s.stop==120
    assert s.on_bar(Bar(bar(37).time,D(90),D(95),D(85),D(90))) is None
    assert s.stop==120 and not s.doji_stop_tightened
