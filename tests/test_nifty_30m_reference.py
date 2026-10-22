from datetime import datetime,timedelta
from decimal import Decimal as D
import pytest
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_30m_reference import IST,Nifty30mReference
from backtest.nifty_30m_reference import run

START=datetime(2026,10,21,9,15,tzinfo=IST)
def bar(i,close=100,opening=None):
    c=D(str(close));o=c if opening is None else D(str(opening))
    return Bar(START+timedelta(minutes=i),o,max(o,c),min(o,c),c)

def opening(s):
    for i in range(30):
        assert s.on_bar(bar(i,100,200 if i==0 else None)) is None
    assert (s.ce_reference,s.pe_reference)==(155,145)


def test_opening_entry_fixed_stop_and_reentry_requires_cross():
    s=Nifty30mReference();opening(s)
    assert s.on_bar(bar(30,100)).action=='BUY_PE'
    s.confirm_fill(index_price=99)
    assert s.stop==119 and s.entry_count==1
    assert s.on_bar(bar(31,119)).reason=='fixed_stop'
    s.confirm_fill()
    assert s.on_bar(bar(32,100)) is None  # Still below, not a new breach.
    assert s.on_bar(bar(33,150)) is None  # Inside opening band.
    assert s.on_bar(bar(34,140)).action=='BUY_PE'


@pytest.mark.parametrize('side,op,cl,expected',[('PE',100,50,105),('CE',100,150,95),
 ('PE',100,20,105),('PE',100,10,65),('CE',100,190,135)])
def test_reference_formulas_and_colors(side,op,cl,expected):
    s=Nifty30mReference();s.position=side;s.pe_reference=D(1000);s.ce_reference=D(1)
    s._update_reference(D(op),D(cl),START)
    assert (s.pe_reference if side=='PE' else s.ce_reference)==expected


def test_small_opposite_and_wider_references_ignored():
    s=Nifty30mReference();s.position='PE';s.pe_reference=D(100)
    for o,c in [(100,60),(100,70),(100,150),(200,150)]:
        s._update_reference(D(o),D(c),START)
        assert s.pe_reference==100
    assert not s.reference_updates


def test_30m_update_only_after_completed_bucket():
    s=Nifty30mReference();opening(s)
    s.on_bar(bar(30,100));s.confirm_fill(index_price=100)
    for i in range(31,59):
        assert s.on_bar(bar(i,70)) is None
        assert s.pe_reference==145
    assert s.on_bar(bar(59,50)) is None
    assert s.pe_reference==105
    assert s.reference_updates[-1]['time']==START+timedelta(minutes=60)
    assert s.on_bar(bar(60,105)).reason=='reference_cross'


def test_four_entries_combined_cap_and_exits_remain_active():
    s=Nifty30mReference(max_entries=4);opening(s)
    s.on_bar(bar(30,100));s.confirm_fill(index_price=100)
    i=31
    for count in range(1,5):
        assert s.on_bar(bar(i,145)).action=='EXIT_PE'
        s.confirm_fill();i+=1
        signal=s.on_bar(bar(i,140));i+=1
        if count<4:
            assert signal.action=='BUY_PE'
            s.confirm_fill(index_price=140)
        else: assert signal is None
    assert s.entry_count==4 and s.position is None


def test_runner_next_open_fills_and_preserves_open_position():
    bars=[bar(i,100,200 if i==0 else None) for i in range(30)]
    bars += [bar(30,100),bar(31,125,opening=99),bar(32,120,opening=127)]
    rows=[dict(date=b.time.isoformat(),open=b.open,high=b.high,low=b.low,close=b.close) for b in bars]
    trades,entry,pending,signals,updates=run(rows,slippage_points=1)
    assert len(trades)==1 and trades[0].entry_price==98 and trades[0].exit_price==128
    assert trades[0].points==-30 and trades[0].initial_risk==20
    assert trades[0].exit_reason=='fixed_stop'
    assert entry is None and pending is None
    trades,entry,pending,*_=run(rows[:32])
    assert entry is not None and pending.action=='EXIT_PE'


def test_missing_data_rejected_and_daily_reset_after_reconciliation():
    s=Nifty30mReference();opening(s)
    with pytest.raises(ValueError,match='Missing'):
        s.on_bar(bar(31))
    s=Nifty30mReference();opening(s)
    s.on_bar(bar(30,100));s.confirm_fill(index_price=100)
    with pytest.raises(ValueError,match='Reconcile'):
        s.on_bar(Bar(START+timedelta(days=1),D(100),D(100),D(100),D(100)))


def test_square_off_without_reference_failure():
    s=Nifty30mReference();opening(s)
    s.on_bar(bar(30,100));s.confirm_fill(index_price=100)
    for i in range(31,360):
        signal=s.on_bar(bar(i,100))
    assert signal.reason=='session_end'
    s.confirm_fill()
    assert s.on_bar(bar(360,100)) is None


@pytest.mark.parametrize('body,wick_range,eligible',[(19,49,False),(19,50,False),(19,51,False),(20,20,True),(19,200,False),(20,200,True),(0,51,False)])
def test_opening_body_filter_ignores_wick_range(body,wick_range,eligible):
    s=Nifty30mReference()
    for i in range(30):
        close=D(100+body if i==29 else 100)
        b=Bar(START+timedelta(minutes=i),D(100),D(100+wick_range),D(100),close)
        assert s.on_bar(b) is None
    assert s.opening_body==body and s.opening_range==wick_range
    assert s.opening_eligible is eligible
    signal=s.on_bar(bar(30,160))
    assert (signal is not None) is eligible
    if not eligible:
        for i in range(31,90):assert s.on_bar(bar(i,200)) is None
        assert s.entry_count==0  # A later large candle cannot unlock the skipped day.


def test_default_three_entries_and_fourth_blocked():
    s=Nifty30mReference();opening(s)
    assert s.max_entries==3
    s.on_bar(bar(30,100));s.confirm_fill(index_price=100)
    for count in range(1,4):
        i=29+count*2
        assert s.on_bar(bar(i,145)).action=='EXIT_PE'
        s.confirm_fill()
        signal=s.on_bar(bar(i+1,140))
        if count<3:
            assert signal.action=='BUY_PE'
            s.confirm_fill(index_price=140)
        else:assert signal is None
    assert s.entry_count==3
