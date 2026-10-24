from datetime import datetime, timedelta
from decimal import Decimal as D
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST
from backtest.fixed_profit_diagnostics import exit_trade, periods


def bar(minute,o,h,l,c):
    return Bar(datetime(2026,10,23,10,tzinfo=IST)+timedelta(minutes=minute),
               *(D(str(p)) for p in (o,h,l,c)))


def test_larger_stop_changes_risk_and_can_turn_loss_into_win():
    rows=[bar(0,100,105,89,102),bar(1,102,132,99,131)]
    assert exit_trade(rows,0,1,10)[:3]==(-10,0,'stop')
    assert exit_trade(rows,0,1,15)[:3]==(30,1,'target')


def test_ambiguous_bar_is_flagged_and_gap_stop_is_worse():
    rows=[bar(0,100,131,89,100)]
    assert exit_trade(rows,0,1)==(-10,0,'stop',True)
    rows=[bar(0,100,105,95,100),bar(1,85,86,80,82)]
    assert exit_trade(rows,0,1)[:3]==(-15,1,'stop')


def test_short_exit_is_symmetric():
    rows=[bar(0,100,111,95,98),bar(1,98,101,68,69)]
    assert exit_trade(rows,0,-1,10)[0]==-10
    assert exit_trade(rows,0,-1,15)[0]==30


def test_costs_and_chronological_split():
    rows=[{'date':'2023-01-01','points':30},{'date':'2024-01-01','points':-10},
          {'date':'2026-01-01','points':30}]
    result=periods(rows)
    assert result['train']['cost_2']['net_points']==28
    assert result['validation']['cost_2']['net_points']==-12
    assert result['test']['gross']['trades']==1


def test_one_minute_follow_through_enters_after_close_not_before():
    from backtest.fixed_profit_diagnostics import features
    rows=[bar(i,120,130,100,120) for i in range(35)]
    rows += [bar(i,131,135,129,133) for i in range(35,40)]
    rows += [bar(40,133,137,132,136),bar(41,136,167,135,166)]
    trade=dict(date='2026-10-23',side='CE',entry_time=rows[40].time.isoformat(),
               reference_high=130,reference_low=100,points=30)
    result=features(rows,trade,None)
    assert result['follow1_points']==30
    # Piercing the rejection wick cancels the delayed setup.
    rows[40]=bar(40,133,137,128,136)
    assert features(rows,trade,None)['follow1_points'] is None
