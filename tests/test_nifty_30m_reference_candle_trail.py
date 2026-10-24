from decimal import Decimal as D
from unittest.mock import patch
import pytest

from strategies.nifty_30m_reference_candle_trail import Nifty30mReferenceCandleTrail
from strategies.nifty_15m_breakout import Bar
from tests.test_nifty_30m_reference import bar, opening
from backtest.nifty_30m_reference import run, print_candle_trail_comparison


@pytest.mark.parametrize('side,entry,near,far,mid,reverse',[
    ('CE',160,170,200,185,184), ('PE',140,130,100,115,116)])
@pytest.mark.parametrize('minutes', [15,30])
def test_completed_matching_candle_tightens_and_reversal_exits(side,entry,near,far,mid,reverse,minutes):
    s=Nifty30mReferenceCandleTrail(candle_minutes=minutes,lookback=1,stop_points=25)
    opening(s)
    s.on_bar(bar(30,entry));s.confirm_fill(index_price=entry)
    initial=s.stop
    # The bucket containing entry is ineligible, even after it closes.
    end_first=45 if minutes==15 else 60
    for i in range(31,end_first):
        assert s.on_bar(bar(i,near)) is None
        assert s.stop==initial
    for i in range(end_first,end_first+minutes):
        assert s.on_bar(bar(i,far,opening=near if i==end_first else None)) is None
        if i<end_first+minutes-1:
            assert s.stop==initial
    assert s.stop==mid
    assert s.trailing_active
    assert len(s.trail_updates)==1
    signal=s.on_bar(bar(end_first+minutes,reverse))
    assert signal.reason=='candle_trail_stop'
    s.confirm_fill()
    assert not s.trailing_active and s.entry_at is None


def test_partial_entry_bucket_cannot_arm_even_on_large_body():
    s=Nifty30mReferenceCandleTrail(candle_minutes=15,lookback=1,stop_points=25)
    opening(s);s.on_bar(bar(30,160));s.confirm_fill(index_price=160)
    for i in range(31,45):
        s.on_bar(bar(i,250))
    assert s.stop==135 and not s.trail_updates


def test_insufficient_prior_history_never_uses_current_body_as_history():
    s=Nifty30mReferenceCandleTrail(candle_minutes=30,lookback=5,stop_points=25)
    opening(s);s.on_bar(bar(30,160));s.confirm_fill(index_price=160)
    for i in range(31,90):
        s.on_bar(bar(i,200))
    assert not s.trail_updates


def test_wicks_do_not_arm_body_trail():
    s=Nifty30mReferenceCandleTrail(candle_minutes=15,lookback=1,stop_points=25)
    opening(s);s.on_bar(bar(30,160));s.confirm_fill(index_price=160)
    for i in range(31,45):s.on_bar(bar(i,170))
    for i in range(45,60):
        b=bar(i,170)
        s.on_bar(Bar(b.time,D(170),D(400),D(169),D(170)))
    assert not s.trail_updates


@pytest.mark.parametrize('args',[dict(candle_minutes=5),dict(body_multiple=0),
    dict(body_multiple='NaN'),dict(lookback=0)])
def test_bad_parameters(args):
    with pytest.raises(ValueError):Nifty30mReferenceCandleTrail(**args)


def test_experiments_stay_isolated_and_baseline_is_unchanged():
    with pytest.raises(ValueError,match='separately'):
        run([],candle_trail_minutes=15,trail_step_points=10)
    with pytest.raises(ValueError,match='separately'):
        run([],candle_trail_minutes=15,ce_color_filter=True)
    rows=[dict(date=bar(i,100,200 if i==0 else None).time.isoformat(),
               open=bar(i,100,200 if i==0 else None).open,high=D(200),low=D(100),close=D(100)) for i in range(30)]
    assert run(rows)==run(rows,candle_trail_minutes=0)


def test_comparison_tracks_both_timeframes_and_top_winner_retention(capsys):
    with patch('backtest.nifty_30m_reference.run',return_value=([],None,None,[],[])) as runner:
        print_candle_trail_comparison([],stop_points=25,max_entries=1)
    assert [call.kwargs['candle_trail_minutes'] for call in runner.call_args_list]==[0,15,30]
    output=capsys.readouterr().out
    assert 'TOP-WINNER RETENTION: candle15' in output
    assert 'TOP-WINNER RETENTION: candle30' in output
    assert 'YEARLY BREAKDOWN' in output and 'MONTHLY BREAKDOWN' in output


def test_opposite_candle_and_lower_midpoint_never_loosen_stop():
    s=Nifty30mReferenceCandleTrail(candle_minutes=15,lookback=1,stop_points=25)
    opening(s);s.on_bar(bar(30,160));s.confirm_fill(index_price=160)
    for i in range(31,45):s.on_bar(bar(i,170))
    for i in range(45,60):s.on_bar(bar(i,200,opening=170 if i==45 else None))
    assert s.stop==185
    for i in range(60,75):s.on_bar(bar(i,195,opening=200 if i==60 else None))
    assert s.stop==185
    for i in range(75,90):s.on_bar(bar(i,190,opening=160 if i==75 else None))
    assert s.stop==185


def test_backtest_candle_signal_fills_next_open_and_preserves_initial_risk():
    bars=[bar(i,100,200 if i==0 else None) for i in range(30)]
    bars += [bar(30,160),bar(31,170,opening=160)]
    bars += [bar(i,170) for i in range(32,45)]
    bars += [bar(i,200,opening=170 if i==45 else None) for i in range(45,60)]
    bars += [bar(60,184),bar(61,182)]
    rows=[dict(date=b.time.isoformat(),open=b.open,high=b.high,low=b.low,close=b.close) for b in bars]
    trades,*_=run(rows,stop_points=25,slippage_points=1,candle_trail_minutes=15,candle_trail_lookback=1)
    assert len(trades)==1
    assert trades[0].exit_reason=='candle_trail_stop'
    assert trades[0].exit_time==bar(61).time
    assert trades[0].entry_price==161 and trades[0].exit_price==181
    assert trades[0].points==20 and trades[0].initial_risk==25
