from decimal import Decimal as D
import pytest
from strategies.nifty_30m_reference import Nifty30mReference
from strategies.nifty_30m_reference_buffered import Nifty30mReferenceBuffered
from tests.test_nifty_30m_reference import bar,opening
from backtest.nifty_30m_reference import run,print_reference_comparison


@pytest.mark.parametrize('side,entry,reference,boundary',[('PE',140,145,150),('CE',160,155,150)])
def test_only_exit_boundary_changes(side,entry,reference,boundary):
    baseline=Nifty30mReference(stop_points=25)
    variant=Nifty30mReferenceBuffered(stop_points=25)
    for s in (baseline,variant):opening(s)
    a,b=baseline.on_bar(bar(30,entry)),variant.on_bar(bar(30,entry))
    assert a==b and a.action=='BUY_'+side
    for s in (baseline,variant):s.confirm_fill(index_price=entry)
    assert baseline.stop==variant.stop
    assert baseline.on_bar(bar(31,reference)).reason=='reference_cross'
    assert variant.on_bar(bar(31,reference)) is None
    assert variant.on_bar(bar(32,boundary)).reason=='reference_cross'
    assert (variant.ce_reference,variant.pe_reference)==(155,145)


def test_fixed_stop_and_square_off_not_delayed():
    s=Nifty30mReferenceBuffered(stop_points=25);opening(s)
    s.on_bar(bar(30,140));s.confirm_fill(index_price=140)
    assert s.on_bar(bar(31,165)).reason=='fixed_stop'
    s=Nifty30mReferenceBuffered(stop_points=25);opening(s)
    s.on_bar(bar(30,140));s.confirm_fill(index_price=140)
    for i in range(31,360):signal=s.on_bar(bar(i,140))
    assert signal.reason=='session_end'


def test_zero_buffer_matches_baseline_and_comparison_is_labeled(capsys):
    bars=[bar(i,100,200 if i==0 else None) for i in range(30)]
    bars += [bar(30,140),bar(31,145,opening=140),bar(32,150),bar(33,151)]
    rows=[dict(date=b.time.isoformat(),open=b.open,high=b.high,low=b.low,close=b.close) for b in bars]
    assert run(rows)==run(rows,reference_exit_buffer=0)
    print_reference_comparison(rows,stop_points=25,max_entries=3,slippage_points=1)
    out=capsys.readouterr().out
    assert 'v3,' in out and 'v3b,' in out and 'only reference-exit buffer changes' in out


@pytest.mark.parametrize('buffer',[-1,'NaN','Infinity'])
def test_invalid_buffer(buffer):
    with pytest.raises(ValueError,match='buffer'):
        Nifty30mReferenceBuffered(reference_exit_buffer=buffer)
