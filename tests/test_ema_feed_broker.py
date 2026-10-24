import os
os.environ.setdefault('DB_ENCRYPTION_KEY','unit-test-key-not-used-for-db')
import unittest
from datetime import datetime,date,time,timedelta
from decimal import Decimal
from unittest.mock import Mock,patch
from zoneinfo import ZoneInfo
from trading.ema_feed import Feed,schedule,completed_today
from trading.ema_broker import KiteBroker
from strategies.nifty_daily_ema_monthly import Settings
from trading.ema_execution import ReconciliationRequired

IST=ZoneInfo('Asia/Kolkata')


class Calendar:
    def next_open(self,day):
        day+=timedelta(days=1)
        while day.weekday()>=5:day+=timedelta(days=1)
        return datetime.combine(day,time(9,15),IST)


class FeedAndBrokerTests(unittest.TestCase):
    def test_skipping_old_gaps_cannot_skip_latest_session(self):
        now=datetime(2026,10,23,9,15,tzinfo=IST)
        with patch('trading.ema_feed.load_snapshot_candles',return_value=[]), \
             patch('trading.ema_feed.daily_from_minutes',return_value=[dict(date=date(2026,10,21))]):
            with self.assertRaisesRegex(ValueError,'Latest completed session is missing'):
                Feed(Mock(),Mock(),'daily_calls',lambda d:d.weekday()<5,now,skip_incomplete_days=True)

    def test_final_and_daily_signals_wait_for_next_session_open(self):
        friday=datetime(2026,10,23,15,30,tzinfo=IST)
        for kind in ('daily_calls','bearish_puts'):
            opening,deadline=schedule(friday,kind,Calendar())
            self.assertEqual(datetime(2026,10,26,9,15,tzinfo=IST),opening)
            self.assertGreater(deadline,opening)
        stamp=datetime(2026,10,23,10,15,tzinfo=IST)
        self.assertEqual(stamp,schedule(stamp,'bearish_puts',Calendar())[0])

    def test_current_minute_finalisation_and_missing_minutes(self):
        start=datetime(2026,10,23,9,15,tzinfo=IST)
        rows=[dict(date=start+timedelta(minutes=i),open=100,high=101,low=99,close=100) for i in range(17)]
        now=start+timedelta(minutes=16,seconds=2)
        self.assertEqual(16,len(completed_today(rows,now)))
        with self.assertRaises(ValueError):completed_today(rows[1:],now)
        with self.assertRaises(ValueError):completed_today(rows[:10],now)

    def test_live_adapter_cap_cash_and_ownership_prechecks(self):
        now=datetime(2026,10,23,10,15,tzinfo=IST)
        kite=Mock();kite.positions.return_value={'net':[]};kite.orders.return_value=[]
        kite.margins.return_value={'net':50000,'available':{'cash':50000,'live_balance':50000}}
        kite.quote.return_value={'NFO:TESTPE':dict(timestamp=now,depth={
            'sell':[dict(price=450,quantity=65)],'buy':[dict(price=445,quantity=65)]})}
        broker=KiteBroker(kite,Mock())
        c=dict(tradingsymbol='TESTPE',product='NRML')
        broker.check_entry(c,65,now,Settings(Decimal('30000')))
        kite.margins.return_value['net']=100
        with self.assertRaises(ValueError):broker.check_entry(c,65,now,Settings(Decimal('30000')))
        kite.margins.return_value['net']=50000
        kite.positions.return_value={'net':[dict(exchange='NFO',tradingsymbol='TESTPE',product='NRML',quantity=65)]}
        with self.assertRaises(ReconciliationRequired):broker.check_entry(c,65,now,Settings(Decimal('30000')))
        broker.check_exit(c,65,now,Settings(Decimal('30000')))
        with self.assertRaises(ReconciliationRequired):broker.check_exit(c,130,now,Settings(Decimal('30000')))
        kite.place_order.assert_not_called()

    def test_buy_limit_enforces_total_premium_cap(self):
        kite=Mock();kite.place_order.return_value='mock-order'
        broker=KiteBroker(kite,Mock())
        broker.submit({'tradingsymbol':'TESTPE','tick_size':'0.05'},'BUY',65,'mock-tag',datetime.now(IST))
        args=kite.place_order.call_args.kwargs
        self.assertEqual('LIMIT',args['order_type'])
        self.assertLessEqual(Decimal(str(args['price']))*args['quantity'],Decimal('30000'))


if __name__=='__main__':unittest.main()
