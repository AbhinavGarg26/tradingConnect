import unittest
import os
os.environ.setdefault("DB_ENCRYPTION_KEY", "unit-test-key-not-used-for-db")
from datetime import date,datetime,timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import Mock

from strategies.nifty_30m_reference import IST,ReferenceSignal
from nifty_30m_reference_live import startup_allowed,completed_bars
from trading.reference_live_execution import (select_offset_contract,confirm_market_order,
    ConfirmedInsufficientFunds,UncertainOrder,ReferenceExecutor,fresh_price,TAG)

TODAY=date(2026,10,23)
NOW=datetime(2026,10,23,9,46,tzinfo=IST)


def catalog():
    return [dict(name='NIFTY',instrument_type=side,expiry=expiry,strike=strike,
                 tradingsymbol=f'NIFTY{expiry:%m%d}{strike}{side}',lot_size=65,tick_size=.05)
            for expiry in [date(2026,10,20),date(2026,10,27),date(2026,11,3)]
            for side in ('CE','PE') for strike in range(22500,23550,50)]


def signal(action='BUY_CE'):
    return ReferenceSignal(action,NOW,D(23020),'opening_break',D(23000),D(22990))


class MarginException(Exception):pass


class FakeKite:
    VARIETY_REGULAR='regular';PRODUCT_MIS='MIS';ORDER_TYPE_MARKET='MARKET'
    VALIDITY_DAY='DAY';TRANSACTION_TYPE_BUY='BUY';TRANSACTION_TYPE_SELL='SELL'
    def __init__(self,mode='complete'):
        self.mode=mode;self.sent=[];self.net=[];self.open_orders=[]
    def positions(self):return {'net':self.net}
    def orders(self):return self.open_orders
    def quote(self,symbols):
        return {s:dict(last_price=23020 if s=='NSE:NIFTY 50' else 120,timestamp=NOW) for s in symbols}
    def place_order(self,**kwargs):
        self.sent.append(kwargs)
        if self.mode=='margin_exception':raise MarginException('Insufficient funds')
        if self.mode=='network':raise TimeoutError('response lost')
        return 'order'+str(len(self.sent))
    def order_history(self,order_id):
        if self.mode=='funds_rejected':return [dict(status='REJECTED',filled_quantity=0,pending_quantity=0,status_message='Insufficient funds. Required margin exceeds available')]
        if self.mode=='partial_rejected':return [dict(status='REJECTED',filled_quantity=1,pending_quantity=0,status_message='Insufficient funds')]
        if self.mode=='cancelled_funds':return [dict(status='CANCELLED',filled_quantity=0,pending_quantity=0,status_message='Insufficient funds')]
        if self.mode=='pending':return [dict(status='OPEN',filled_quantity=0)]
        p=self.sent[-1]
        self.net=[dict(exchange='NFO',tradingsymbol=p['tradingsymbol'],quantity=p['quantity'] if p['transaction_type']=='BUY' else 0,product='MIS')]
        return [dict(status='COMPLETE',filled_quantity=p['quantity'],average_price=120)]


class Engine:
    def __init__(self):self.entry_count=0;self.calls=[]
    def confirm_fill(self,**kwargs):
        self.calls.append(kwargs)
        if kwargs:self.entry_count+=1


class SelectionTests(unittest.TestCase):
    def test_offsets_current_expiry(self):
        for side,strike in [('CE',23100),('PE',22900)]:
            c=select_offset_contract(catalog(),TODAY,side,D(23020),[],[])
            self.assertEqual(c['strike'],strike)
            self.assertEqual(c['expiry'],date(2026,10,27))
    def test_conflict_any_side_searches_lower_and_pending_blocks(self):
        cat=catalog();occupied=next(c for c in cat if c['strike']==23100 and c['instrument_type']=='PE' and c['expiry']==date(2026,11,3))
        positions=[dict(exchange='NFO',tradingsymbol=occupied['tradingsymbol'],quantity=-65)]
        pending=next(c for c in cat if c['strike']==23050 and c['instrument_type']=='CE' and c['expiry']==date(2026,10,27))
        orders=[dict(exchange='NFO',tradingsymbol=pending['tradingsymbol'],status='OPEN')]
        c=select_offset_contract(cat,TODAY,'CE',D(23020),positions,orders)
        self.assertEqual(c['strike'],23000)
    def test_pe_conflict_goes_lower_not_up(self):
        cat=catalog();c=select_offset_contract(cat,TODAY,'PE',D(23020),[],[])
        out=select_offset_contract(cat,TODAY,'PE',D(23020),[dict(exchange='NFO',tradingsymbol=c['tradingsymbol'],quantity=65)],[])
        self.assertEqual(out['strike'],22850)
    def test_missing_offset_rejected(self):
        with self.assertRaises(ValueError):select_offset_contract(catalog(),TODAY,'PE',D(22500),[],[])


class OrderTests(unittest.TestCase):
    def order(self,kite,exit_order=False):
        c=select_offset_contract(catalog(),TODAY,'CE',D(23020),[],[])
        return confirm_market_order(kite,c,'SELL' if exit_order else 'BUY',65,Mock(),exit_order=exit_order,sleep_fn=lambda _:None,attempts=2)
    def test_confirmed_full_fill_only(self):
        kite=FakeKite();oid,premium=self.order(kite)
        self.assertEqual((oid,premium),('order1',120))
        self.assertEqual(kite.sent[0]['tag'],TAG)
        self.assertEqual(kite.sent[0]['market_protection'],-1)
    def test_confirmed_funds_rejections(self):
        for mode in ('margin_exception','funds_rejected'):
            with self.subTest(mode=mode),self.assertRaises(ConfirmedInsufficientFunds):self.order(FakeKite(mode))
    def test_no_fallback_for_partial_cancel_network_timeout(self):
        for mode in ('partial_rejected','cancelled_funds','network','pending'):
            with self.subTest(mode=mode),self.assertRaises(UncertainOrder):self.order(FakeKite(mode))
    def test_funds_exit_never_becomes_paper(self):
        with self.assertRaises(UncertainOrder):self.order(FakeKite('funds_rejected'),exit_order=True)
    def test_journal_failure_does_not_retry(self):
        k=FakeKite();j=Mock();j.order_submitted.side_effect=RuntimeError('DB offline')
        c=select_offset_contract(catalog(),TODAY,'CE',D(23020),[],[])
        with self.assertRaises(UncertainOrder):confirm_market_order(k,c,'BUY',65,j,sleep_fn=lambda _:None)
        self.assertEqual(len(k.sent),1)


class ExecutionTests(unittest.TestCase):
    def make(self,mode='complete',live=True):
        k=FakeKite(mode);j=Mock();e=Engine()
        x=ReferenceExecutor(k,catalog(),j,live=live,now_fn=lambda:NOW)
        return k,j,e,x
    def test_live_entry_exit_and_owned_quantity(self):
        k,j,e,x=self.make();x.handle(signal(),e)
        self.assertFalse(x.position.paper)
        self.assertEqual(e.entry_count,1)
        x.handle(signal('EXIT_CE'),e)
        self.assertIsNone(x.position)
        self.assertEqual([o['transaction_type'] for o in k.sent],['BUY','SELL'])
    def test_confirmed_margin_falls_back_to_paper_and_never_sells(self):
        k,j,e,x=self.make('funds_rejected');x.handle(signal(),e)
        self.assertTrue(x.position.paper)
        self.assertTrue(j.entry_filled.call_args.kwargs['paper'])
        self.assertEqual(e.entry_count,1)
        x.handle(signal('EXIT_CE'),e)
        self.assertEqual(len(k.sent),1)
    def test_paper_mode_never_places_order(self):
        k,j,e,x=self.make(live=False);x.handle(signal(),e);x.handle(signal('EXIT_CE'),e)
        self.assertEqual(k.sent,[])
    def test_uncertain_entry_never_falls_back(self):
        k,j,e,x=self.make('network')
        with self.assertRaises(UncertainOrder):x.handle(signal(),e)
        j.entry_filled.assert_not_called();self.assertEqual(e.entry_count,0)
    def test_manual_added_lot_prevents_exit(self):
        k,j,e,x=self.make();x.handle(signal(),e)
        k.net[0]['quantity']=130
        with self.assertRaises(UncertainOrder):x.handle(signal('EXIT_CE'),e)
        self.assertEqual(len(k.sent),1)
    def test_position_after_funds_rejection_forbids_paper(self):
        k,j,e,x=self.make('funds_rejected')
        old=k.order_history
        def history(oid):
            out=old(oid)
            k.net=[dict(exchange='NFO',tradingsymbol=k.sent[-1]['tradingsymbol'],quantity=65,product='MIS')]
            return out
        k.order_history=history
        with self.assertRaises(UncertainOrder):x.handle(signal(),e)
        j.entry_filled.assert_not_called()


class SessionTests(unittest.TestCase):
    def test_tomorrow_and_startup_window(self):
        start=date(2026,10,23)
        self.assertFalse(startup_allowed(datetime(2026,10,22,9,15,tzinfo=IST),start))
        self.assertTrue(startup_allowed(datetime(2026,10,23,9,15,tzinfo=IST),start))
        self.assertFalse(startup_allowed(datetime(2026,10,23,9,45,tzinfo=IST),start))
        self.assertFalse(startup_allowed(datetime(2026,10,24,9,15,tzinfo=IST),start))
    def test_only_completed_1m_bars(self):
        r=dict(date=NOW,open=100,high=101,low=99,close=100)
        self.assertEqual(completed_bars([r],NOW+timedelta(minutes=1,seconds=1)),[])
        self.assertEqual(len(completed_bars([r],NOW+timedelta(minutes=1,seconds=2))),1)
    def test_stale_quote_refused(self):
        with self.assertRaises(RuntimeError):fresh_price(FakeKite(),'NSE:NIFTY 50',NOW+timedelta(minutes=1))


if __name__=='__main__':unittest.main()


class FirstPEPresetTest(unittest.TestCase):
    def test_preset_uses_frozen_selection_and_limits(self):
        from nifty_30m_reference_live import configured_engine
        from strategies.nifty_30m_reference_first_pe import Nifty30mReferenceFirstPE
        engine=configured_engine('pe1-v1')
        self.assertIsInstance(engine,Nifty30mReferenceFirstPE)
        self.assertEqual(engine.stop_points,25)
        self.assertEqual(engine.max_entries,1)
        self.assertEqual(engine.buffer_percent,5)
        self.assertEqual(configured_engine('v3').max_entries,3)

    def test_unknown_preset_rejected(self):
        from nifty_30m_reference_live import validate_execution_mode
        with self.assertRaises(ValueError):
            validate_execution_mode('unknown',True)
