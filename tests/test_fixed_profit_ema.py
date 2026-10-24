from datetime import datetime, timedelta
from decimal import Decimal as D
import pytest

from strategies.fixed_profit_ema import FixedProfitEMA
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST
from backtest.fixed_profit_ema import ClosedEMA, trend_schedule, run_variant

START=datetime(2026,10,23,9,15,tzinfo=IST)


def bar(minute,o,h,l,c):
    return Bar(START+timedelta(minutes=minute),*(D(str(p)) for p in (o,h,l,c)))


def engine(**kwargs):
    e=FixedProfitEMA(**kwargs)
    e.set_reference(bar(0,110,150,100,120),observed_at=START+timedelta(minutes=30))
    return e


def feed(e,b):
    return e.on_bar(b,observed_at=b.time+timedelta(minutes=5))


@pytest.mark.parametrize('side',('CE','PE'))
def test_rejection_inside_range_and_ema_direction(side):
    e=engine()
    e.set_trend(95 if side=='CE' else 155,94 if side=='CE' else 156,
                closed_at=START+timedelta(minutes=30))
    b=bar(30,101,107,99,105) if side=='CE' else bar(30,149,151,143,145)
    assert feed(e,b).action=='BUY_'+side
    fill=105 if side=='CE' else 145
    e.confirm_entry(index_price=fill)
    direction=1 if side=='CE' else -1
    assert e.target==fill+direction*30
    assert e.stop==fill-direction*10


def test_wrong_side_ema_or_outside_close_has_no_entry():
    e=engine()
    e.set_trend(110,109,closed_at=START+timedelta(minutes=30))
    assert feed(e,bar(30,101,107,99,105)) is None
    e=engine(require_ema=False)
    assert feed(e,bar(30,99,102,98,99)) is None


def test_slope_optional_and_actual_fill_rechecked():
    e=engine(require_slope=True)
    e.set_trend(95,96,closed_at=START+timedelta(minutes=30))
    assert feed(e,bar(30,101,107,99,105)) is None
    e=engine()
    e.set_trend(103,102,closed_at=START+timedelta(minutes=30))
    assert feed(e,bar(30,101,107,99,105))
    with pytest.raises(ValueError,match='EMA'):
        e.confirm_entry(index_price=102)


def test_future_ema_and_unclosed_candles_are_rejected():
    e=engine()
    b=bar(30,101,107,99,105)
    with pytest.raises(ValueError,match='closed'):
        e.on_bar(b,observed_at=b.time)
    e.set_trend(95,94,closed_at=START+timedelta(minutes=60))
    with pytest.raises(ValueError,match='Future'):
        feed(e,b)


def test_ema_sma_seed_and_closed_bar_order():
    ema=ClosedEMA(3)
    for i,c in enumerate((10,20,30)):
        ema.update(c,START+timedelta(minutes=30*i))
    assert ema.value==20
    ema.update(40,START+timedelta(minutes=90))
    assert ema.value==30 and ema.previous==20
    with pytest.raises(ValueError,match='chronological'):
        ema.update(50,START+timedelta(minutes=90))


def test_schedule_carries_across_days_and_never_uses_partial_last_bar():
    sessions=[]
    for day in range(11):
        bars=[Bar(START+timedelta(days=day,minutes=i),D(100),D(101),D(99),D(100)) for i in range(375)]
        sessions.append((bars[0].time.date(),bars))
    schedule,_=trend_schedule(sessions)
    assert schedule
    assert all(t.time().minute in {15,45} for t in schedule)
    assert all(t.time()<=datetime.strptime('15:15','%H:%M').time() for t in schedule)
    first=min(schedule)
    assert first==START+timedelta(days=8,minutes=360)
    for period,(value,previous,closed_at) in schedule[max(schedule)].items():
        assert value==previous==100 and closed_at==max(schedule)


def test_reentry_control_uses_next_open_and_checks_entry_minute_exit():
    rows=[bar(i,110,150,100,120) for i in range(30)]
    rows += [bar(i,101,107,99,105) for i in range(30,35)]
    rows += [bar(35,104,135,103,134)]
    trade=run_variant(rows,{},None)
    assert trade['entry']==104 and trade['points']==30
    assert trade['entry_time']==rows[35].time.isoformat()


def test_later_session_prices_cannot_change_an_earlier_ema():
    sessions=[]
    for day in range(11):
        bars=[Bar(START+timedelta(days=day,minutes=i),D(100),D(101),D(99),D(100)) for i in range(375)]
        sessions.append((bars[0].time.date(),bars))
    original,_=trend_schedule(sessions)
    last_day,bars=sessions[-1]
    sessions[-1]=(last_day,[Bar(b.time,D(1000),D(1001),D(999),D(1000)) for b in bars])
    changed,_=trend_schedule(sessions)
    for timestamp,values in original.items():
        if timestamp.date()<last_day:
            assert changed[timestamp]==values
