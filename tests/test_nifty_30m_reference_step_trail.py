from decimal import Decimal as D
import pytest
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_30m_reference_step_trail import Nifty30mReferenceStepTrail
from tests.test_nifty_30m_reference import bar,opening
from backtest.nifty_30m_reference import run,print_step_trail_comparison


@pytest.mark.parametrize('side,entry,direction',[('CE',160,1),('PE',140,-1)])
def test_step_boundaries_skip_multiple_steps_never_loosen_and_exit(side,entry,direction):
    s=Nifty30mReferenceStepTrail(stop_points=25)
    opening(s);s.on_bar(bar(30,entry));s.confirm_fill(index_price=entry)
    initial=s.stop
    assert s.on_bar(bar(31,D(entry)+D('9.99')*direction)) is None
    assert s.stop==initial
    assert s.on_bar(bar(32,entry+10*direction)) is None
    assert s.stop==entry
    assert s.on_bar(bar(33,entry+20*direction)) is None
    assert s.stop==entry+10*direction
    assert s.on_bar(bar(34,entry+35*direction)) is None
    assert s.stop==entry+20*direction
    assert s.on_bar(bar(35,entry+25*direction)) is None
    assert s.stop==entry+20*direction
    signal=s.on_bar(bar(36,entry+20*direction))
    assert signal.action=='EXIT_'+side and signal.reason=='step_trail_stop'
    s.confirm_fill()
    assert s.entry_price is None and not s.trailing_active


def test_intrabar_high_does_not_arm_close_based_trail_and_reference_exit_remains():
    s=Nifty30mReferenceStepTrail(stop_points=25);opening(s)
    s.on_bar(bar(30,160));s.confirm_fill(index_price=161)
    assert s.on_bar(Bar(bar(31).time,D(161),D(200),D(159),D(161))) is None
    assert s.stop==136 and not s.trailing_active
    assert s.on_bar(bar(32,154)).reason=='reference_cross'


def test_initial_stop_and_reentry_reset_are_preserved():
    s=Nifty30mReferenceStepTrail(stop_points=25);opening(s)
    s.on_bar(bar(30,160));s.confirm_fill(index_price=161)
    assert s.on_bar(bar(31,136)).reason=='fixed_stop'
    s.confirm_fill()
    s.on_bar(bar(32,160));s.confirm_fill(index_price=162)
    assert s.entry_price==162 and s.stop==137 and not s.trailing_active


def test_backtest_can_lose_after_breakeven_step(capsys):
    bars=[bar(i,100,200 if i==0 else None) for i in range(30)]
    bars += [bar(30,160),bar(31,171,opening=160),bar(32,161),bar(33,155,opening=158)]
    rows=[dict(date=b.time.isoformat(),open=b.open,high=b.high,low=b.low,close=b.close) for b in bars]
    trades,*_=run(rows,stop_points=25,slippage_points=1,trail_step_points=10)
    assert trades[0].initial_risk==25
    assert trades[0].exit_reason=='step_trail_stop' and trades[0].points==-4
    print_step_trail_comparison(rows,stop_points=25,slippage_points=1)
    assert 'v3d,' in capsys.readouterr().out


@pytest.mark.parametrize('step',[0,-1,'NaN','Infinity'])
def test_invalid_step(step):
    with pytest.raises(ValueError,match='Trail step'):
        Nifty30mReferenceStepTrail(trail_step_points=step)


def test_combined_experiments_disallowed():
    with pytest.raises(ValueError,match='separately'):
        run([],trail_step_points=10,ce_color_filter=True)
