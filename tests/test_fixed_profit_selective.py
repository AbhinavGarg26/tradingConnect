from datetime import datetime, timedelta
from decimal import Decimal as D
import pytest
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST
from strategies.fixed_profit_selective import evaluate_entry
from backtest.fixed_profit_improve import filter_rules, analyze

START=datetime(2026,10,23,9,15,tzinfo=IST)


def bars(count=14,wide=False,chop=False):
    result=[]
    for i in range(count):
        close=D(101 if not chop or i%2 else 99)
        result.append(Bar(START+timedelta(minutes=i*5),D(100),D(115 if wide else 102),
                          D(85 if wide else 98),close))
    return result


def decide(values):
    return evaluate_entry(values,100,observed_at=values[-1].time+timedelta(minutes=5))


def test_clean_lower_volatility_passes_and_warmup_does_not():
    decision=decide(bars())
    assert decision.allowed and decision.atr==4 and decision.crossings==0
    assert not decide(bars(13)).allowed


def test_volatile_or_repeatedly_crossed_level_rejected():
    assert not decide(bars(wide=True)).allowed
    decision=decide(bars(chop=True))
    assert not decision.allowed and decision.crossings==7


def test_missing_or_future_candles_are_not_treated_as_valid():
    values=bars();del values[5]
    assert not decide(values).allowed
    with pytest.raises(ValueError,match='completed'):
        evaluate_entry(bars(),100,observed_at=bars()[-1].time)


def test_rule_does_not_depend_on_future_profit():
    rule=filter_rules()['low_volatility_clean']
    row=dict(atr_ready=True,atr5=15,crossings=1,stop15_points=-15)
    assert rule(row)
    row['stop15_points']=30
    assert rule(row)


def test_saved_exports_use_fifteen_stop_outcomes(tmp_path):
    import csv
    source=tmp_path/'source.csv'
    row=dict(date='2023-01-01',side='CE',entry_time='2023-01-01T11:00:00+05:30',
             entry=100,target=130,points=-10,stop15_points=30,atr5=15,crossings=1,
             entry_distance=3,body_fraction=.3,close_location=.5,early=True,atr_ready=True)
    with source.open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
    output=tmp_path/'result'
    report=analyze(source,output)
    exported=list(csv.DictReader((output/'baseline_entries.csv').open()))[0]
    assert float(exported['stop'])==85
    assert float(exported['points'])==30
    assert float(exported['original_10_stop_points'])==-10
    assert report['selected_on_training'] is None
    assert not report['passed_research_gate']
