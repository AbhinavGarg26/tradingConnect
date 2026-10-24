import os
os.environ.setdefault("DB_ENCRYPTION_KEY", "unit-test-key-not-used-for-db")
from datetime import datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
import json
import pytest

from strategies.fixed_profit_filtered import FilteredSession, CONFIG
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST
from trading.fixed_profit_execution import FixedProfitExecutor, confirmed_order, TAG, UncertainOrder
from trading.fixed_profit_journal import SessionJournal
from fixed_profit_filtered_live import completed_minutes, startup_allowed

START=datetime(2026,10,23,9,15,tzinfo=IST)
NOW=START+timedelta(minutes=70,seconds=3)


def make_session(breakout=60):
    session=FilteredSession()
    for i in range(breakout+10):
        o,h,l,c=(130,160,100,150) if i==0 else (150,151,149,150)
        if breakout<=i<breakout+5:o,h,l,c=159,165,159,163
        if i>=breakout+5:o,h,l,c=162,165,159,164
        bar=Bar(START+timedelta(minutes=i),*(D(p) for p in (o,h,l,c)))
        session.on_minute(bar,observed_at=bar.time+timedelta(minutes=1))
    return session


class Journal:
    def __init__(self):self.events=[]
    def event(self,action,**details):self.events.append((action,details))


class Kite:
    VARIETY_REGULAR='regular';PRODUCT_MIS='MIS';ORDER_TYPE_MARKET='MARKET'
    VALIDITY_DAY='DAY';TRANSACTION_TYPE_BUY='BUY';TRANSACTION_TYPE_SELL='SELL'
    def __init__(self,mode='complete'):
        self.sent=[];self.net=[];self.mode=mode
    def positions(self):return {'net':self.net}
    def orders(self):return []
    def quote(self,symbols):return {s:dict(last_price=164 if s=='NSE:NIFTY 50' else 120,timestamp=NOW) for s in symbols}
    def place_order(self,**kwargs):
        self.sent.append(kwargs)
        if self.mode=='network':raise TimeoutError('unknown outcome')
        return 'order'+str(len(self.sent))
    def order_history(self,order_id):
        p=self.sent[-1]
        if self.mode=='partial':return [dict(status='COMPLETE',filled_quantity=1,average_price=120)]
        self.net=[dict(exchange='NFO',tradingsymbol=p['tradingsymbol'],product='MIS',
                       quantity=p['quantity'] if p['transaction_type']=='BUY' else 0)]
        return [dict(status='COMPLETE',filled_quantity=p['quantity'],average_price=120)]


def catalog():
    return [dict(name='NIFTY',instrument_type=side,expiry=NOW.date()+timedelta(days=4),
                 strike=strike,tradingsymbol=f'N{strike}{side}',lot_size=10)
            for side in ('CE','PE') for strike in (150,160,170)]


def test_controller_preserves_qualification_and_fixed_risk():
    session=make_session()
    assert session.engine.pending.action=='BUY_CE'
    assert session.qualify_entry(164,observed_at=NOW)
    session.engine.confirm_entry(index_price=164)
    assert session.engine.stop==149 and session.engine.target==194
    with pytest.raises(TypeError):CONFIG['stop_points']=20


def test_first_original_entry_before_atr_warmup_skips_entire_day():
    session=make_session(30)
    assert not session.qualify_entry(164,observed_at=START+timedelta(minutes=40))
    assert session.skipped and session.engine.pending is None


def test_invalid_fill_is_rejected_without_changing_cohort():
    session=make_session()
    assert not session.qualify_entry(180,observed_at=NOW)
    assert not session.skipped


def test_stale_entry_never_catches_up_and_missing_minutes_rejected():
    session=make_session()
    assert not session.qualify_entry(164,observed_at=NOW+timedelta(minutes=1))
    assert session.skipped
    with pytest.raises(ValueError,match='Missing'):
        session.on_minute(Bar(NOW+timedelta(minutes=2),D(164),D(165),D(163),D(164)),observed_at=NOW+timedelta(minutes=4))


def test_paper_entry_exit_never_submits_broker_orders():
    session=make_session();kite=Kite();journal=Journal()
    executor=FixedProfitExecutor(kite,catalog(),journal,now_fn=lambda:NOW)
    assert executor.enter(session)
    assert not kite.sent and executor.position.contract['strike']==160
    signal=session.engine.on_tick(NOW,149)
    executor.exit(signal,session.engine)
    assert not kite.sent and executor.position is None
    assert session.engine.entries==1


def test_simulated_live_roundtrip_has_isolated_tag_and_ownership():
    # This fake broker is entirely in memory: no network or real orders.
    session=make_session();kite=Kite();journal=Journal()
    executor=FixedProfitExecutor(kite,catalog(),journal,live=True,now_fn=lambda:NOW)
    assert executor.enter(session)
    assert kite.sent[0]['tag']==TAG and kite.sent[0]['quantity']==10
    signal=session.engine.on_tick(NOW,149)
    executor.exit(signal,session.engine)
    assert len(kite.sent)==2 and kite.sent[1]['transaction_type']=='SELL'
    assert executor.position is None


def test_uncertain_submission_and_partial_fill_are_never_retried():
    for mode in ('network','partial'):
        kite=Kite(mode)
        with pytest.raises(UncertainOrder):
            confirmed_order(kite,catalog()[0],'BUY',10,Journal(),sleep_fn=lambda _:None)
        assert len(kite.sent)==1


def test_manual_position_change_blocks_automatic_exit():
    session=make_session();kite=Kite();journal=Journal()
    executor=FixedProfitExecutor(kite,catalog(),journal,live=True,now_fn=lambda:NOW)
    executor.enter(session)
    kite.net[0]['quantity']=20
    signal=session.engine.on_tick(NOW,149)
    with pytest.raises(UncertainOrder,match='changed'):executor.exit(signal,session.engine)
    assert len(kite.sent)==1


def test_journal_locks_config_and_blocks_restart_or_unresolved_prior_trade(tmp_path):
    journal=SessionJournal(tmp_path,'test',NOW.date(),live=True)
    journal.event('ENTRY_INTENT',symbol='N160CE')
    with pytest.raises(RuntimeError):SessionJournal(tmp_path,'test',NOW.date(),live=True)
    journal.close()
    record=json.loads(next(tmp_path.glob('*.json')).read_text())
    assert record['config']['stop_points']==15 and record['config']['entry_stop_points']==10
    with pytest.raises(RuntimeError,match='Unresolved'):
        SessionJournal(tmp_path,'test',(NOW+timedelta(days=1)).date(),live=True)


def test_completion_grace_and_start_window():
    stamp=START
    row=dict(date=stamp,open=100,high=101,low=99,close=100)
    assert completed_minutes([row],stamp+timedelta(minutes=1))==[]
    assert len(completed_minutes([row],stamp+timedelta(minutes=1,seconds=2)))==1
    assert startup_allowed(START)
    assert not startup_allowed(START+timedelta(minutes=30))
    assert not startup_allowed(START+timedelta(days=1))
