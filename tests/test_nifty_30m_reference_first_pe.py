import pytest
from strategies.nifty_30m_reference_first_pe import Nifty30mReferenceFirstPE
from tests.test_nifty_30m_reference import bar, opening
from backtest.nifty_30m_reference import run


def test_first_ce_skips_even_if_later_pe_qualifies():
    s=Nifty30mReferenceFirstPE(stop_points=25);opening(s)
    assert s.on_bar(bar(30,160)) is None
    assert s.first_signal_side=='CE'
    assert s.on_bar(bar(31,140)) is None
    assert s.on_bar(bar(32,130)) is None
    assert s.entry_count==0 and s.pending is None


def test_first_pe_uses_baseline_stop_reference_exit_and_one_entry_cap():
    s=Nifty30mReferenceFirstPE(stop_points=25);opening(s)
    assert s.on_bar(bar(30,140)).action=='BUY_PE'
    s.confirm_fill(index_price=140)
    assert s.stop==165
    assert s.on_bar(bar(31,145)).reason=='reference_cross'
    s.confirm_fill()
    assert s.on_bar(bar(32,140)) is None
    assert s.entry_count==1


def test_band_does_not_decide_side_until_first_qualifying_signal():
    s=Nifty30mReferenceFirstPE();opening(s)
    assert s.on_bar(bar(30,150)) is None
    assert s.first_signal_side is None
    assert s.on_bar(bar(31,140)).action=='BUY_PE'


def test_daily_decision_resets():
    from datetime import timedelta
    from strategies.nifty_15m_breakout import Bar
    s=Nifty30mReferenceFirstPE();opening(s);s.on_bar(bar(30,160))
    for i in range(31):
        b=bar(i,100,200 if i==0 else None)
        signal=s.on_bar(Bar(b.time+timedelta(days=1),b.open,b.high,b.low,b.close))
    assert signal.action=='BUY_PE'


def test_invalid_combinations_rejected():
    with pytest.raises(ValueError):Nifty30mReferenceFirstPE(max_entries=3)
    with pytest.raises(ValueError):run([],first_signal_pe=True,max_entries=3)
    with pytest.raises(ValueError):run([],first_signal_pe=True,max_entries=1,trail_step_points=10)


def test_frozen_preset_locks_research_parameters():
    from types import SimpleNamespace
    from backtest.nifty_30m_first_pe_frozen import frozen_arguments
    args=SimpleNamespace(csv_path=None,from_date='2020-01-01',to_date='2026-10-23',
                         summary_only=True,skip_incomplete_days=True)
    argv=frozen_arguments(args)
    for flag,value in [('--stop-points','25'),('--max-entries','1'),
                       ('--buffer-percent','5'),('--slippage-points','1')]:
        assert argv[argv.index(flag)+1]==value
    assert '--first-signal-pe' in argv and '--compare-first-signal-pe' not in argv
