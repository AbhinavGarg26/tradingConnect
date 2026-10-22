from decimal import Decimal as D
import pytest
from strategies.nifty_30m_reference import Nifty30mReference
from strategies.nifty_30m_reference_ce_green import Nifty30mReferenceCEGreen
from tests.test_nifty_30m_reference import bar,opening
from backtest.nifty_30m_reference import run,print_ce_color_comparison


def test_ce_red_signal_rejected_green_can_qualify_later():
    baseline=Nifty30mReference(stop_points=25,max_entries=2)
    variant=Nifty30mReferenceCEGreen(stop_points=25,max_entries=2)
    for s in (baseline,variant):opening(s)
    red=bar(30,160,opening=180)
    assert baseline.on_bar(red).action=='BUY_CE'
    assert variant.on_bar(red) is None
    assert variant.entry_count==0 and variant.position is None
    assert variant.on_bar(bar(31,165,opening=160)).action=='BUY_CE'
    variant.confirm_fill(index_price=166)
    assert variant.entry_count==1 and variant.stop==141


def test_ce_doji_rejected_but_pe_green_unchanged():
    s=Nifty30mReferenceCEGreen();opening(s)
    assert s.on_bar(bar(30,160)) is None
    assert s.on_bar(bar(31,140,opening=130)).action=='BUY_PE'


def test_backtest_filters_signal_not_future_fill_candle():
    bars=[bar(i,100,opening=200 if i==0 else None) for i in range(30)]
    bars += [bar(30,160,opening=150),bar(31,150,opening=162),bar(32,149)]
    rows=[dict(date=b.time.isoformat(),open=b.open,high=b.high,low=b.low,close=b.close) for b in bars]
    a=run(rows,stop_points=25,max_entries=2,slippage_points=1)
    b=run(rows,stop_points=25,max_entries=2,slippage_points=1,ce_color_filter=True)
    assert a==b
    assert b[0][0].entry_price==163  # Red fill candle does not invalidate green signal.
    assert b[0][0].exit_reason=='reference_cross'


def test_comparison_reports_winners_and_disallows_combined_experiments(capsys):
    with pytest.raises(ValueError,match='separately'):
        run([],ce_color_filter=True,reference_exit_buffer=5)
    bars=[bar(i,100,opening=200 if i==0 else None) for i in range(30)]
    bars += [bar(30,160,opening=150),bar(31,150,opening=162),bar(32,149)]
    rows=[dict(date=b.time.isoformat(),open=b.open,high=b.high,low=b.low,close=b.close) for b in bars]
    print_ce_color_comparison(rows,stop_points=25,max_entries=2,slippage_points=1)
    text=capsys.readouterr().out
    assert 'v3c,' in text and 'only CE signal-candle color filter changes' in text
    assert 'BASELINE TOP FIVE WINNERS' in text
