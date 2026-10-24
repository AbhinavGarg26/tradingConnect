import unittest
import os
os.environ.setdefault("DB_ENCRYPTION_KEY", "unit-test-key-not-used-for-db")
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from strategies.nifty_daily_ema_monthly import Settings, action, quote_fill, select_monthly_option, trend

IST = ZoneInfo('Asia/Kolkata')


def weekday(day):
    return day.weekday() < 5


def state(day=date(2026,10,23), bullish=True, up=False, down=False):
    return dict(date=day, bullish=bullish, fast=110 if bullish else 90, slow=100,
                cross_up=up, cross_down=down)


def instruments():
    rows = []
    for expiry in (date(2026,10,27), date(2026,11,24)):
        rows.append(dict(name='NIFTY',instrument_type='FUT',expiry=expiry))
        for side in ('CE','PE'):
            for strike in (24000,24050):
                rows.append(dict(name='NIFTY',instrument_type=side,expiry=expiry,
                                 strike=strike,lot_size=65,tradingsymbol=f'{expiry}{strike}{side}'))
    rows.append(dict(name='NIFTY',instrument_type='CE',expiry=date(2026,10,20),
                     strike=24000,lot_size=65,tradingsymbol='WEEKLY'))
    return rows


class MonthlyEMATests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(Decimal('30000'))

    def test_monthly_contract_uses_futures_not_weekly_and_selects_atm(self):
        c = select_monthly_option(instruments(), date(2026,10,19), 24040, self.settings, weekday, 'CE')
        self.assertEqual(date(2026,10,27), c['expiry'])
        self.assertEqual(24050, c['strike'])
        self.assertEqual('CE', c['instrument_type'])

    def test_roll_window_selects_next_month_and_honours_holidays(self):
        c = select_monthly_option(instruments(), date(2026,10,23), 24000, self.settings, weekday, 'CE')
        self.assertEqual(date(2026,11,24), c['expiry'])
        holiday = lambda d: weekday(d) and d != date(2026,10,26)
        c = select_monthly_option(instruments(), date(2026,10,22), 24000, self.settings, holiday, 'CE')
        self.assertEqual(date(2026,11,24), c['expiry'])

    def test_no_monthly_futures_does_not_guess_expiry(self):
        with self.assertRaises(ValueError):
            select_monthly_option([r for r in instruments() if r['instrument_type']!='FUT'],
                                  date(2026,10,19),24000,self.settings,weekday,'CE')

    def test_bearish_cross_exits_calls_and_stays_in_cash(self):
        today=date(2026,10,26)
        bearish=state(bullish=False,down=True)
        self.assertEqual('wait',action(bearish,None,today,self.settings,weekday))
        self.assertEqual('exit',action(bearish,dict(expiry=date(2026,11,24),side='CE'),today,self.settings,weekday))
        self.assertEqual('exit',action(bearish,dict(expiry=date(2026,11,24),side='PE'),today,self.settings,weekday))
        self.assertEqual('exit',action(state(up=True),dict(expiry=date(2026,11,24),side='PE'),today,self.settings,weekday))

    def test_no_catchup_entry_and_roll_only_owned_side(self):
        today=date(2026,10,26)
        self.assertEqual('wait',action(state(),None,today,self.settings,weekday))
        self.assertEqual('roll',action(state(),dict(expiry=date(2026,10,27),side='CE'),today,self.settings,weekday))
        with self.assertRaises(ValueError):
            action(state(),dict(expiry=date(2026,10,22),side='CE'),today,self.settings,weekday)

    def test_stale_daily_and_current_day_candles_rejected(self):
        for day in (date(2026,10,22),date(2026,10,26)):
            with self.assertRaises(ValueError):
                action(state(day=day),None,date(2026,10,26),self.settings,weekday)

    def test_premium_budget_liquidity_and_bid_ask(self):
        now=datetime(2026,10,26,9,17,tzinfo=IST)
        quote=dict(timestamp=now, depth={'sell':[{'price':450,'quantity':65}],
                                        'buy':[{'price':445,'quantity':65}]})
        self.assertEqual(Decimal(450),quote_fill(quote,buying=True,quantity=65,now=now,settings=self.settings))
        self.assertEqual(Decimal(445),quote_fill(quote,buying=False,quantity=65,now=now,settings=self.settings))
        with self.assertRaisesRegex(ValueError,'quantity'):
            quote_fill(quote,buying=True,quantity=100,now=now,settings=self.settings)  # book size first

    def test_quote_budget_staleness_and_book_depth(self):
        now=datetime(2026,10,26,9,17,tzinfo=IST)
        q=dict(timestamp=now,depth={'sell':[{'price':500,'quantity':65}]})
        with self.assertRaisesRegex(ValueError,'cap'):
            quote_fill(q,buying=True,quantity=65,now=now,settings=self.settings)
        q['timestamp']=now-timedelta(seconds=61)
        with self.assertRaisesRegex(ValueError,'Fresh'):
            quote_fill(q,buying=True,quantity=65,now=now,settings=self.settings)
        q['timestamp']=now
        q['depth']['sell'][0]['quantity']=10
        with self.assertRaisesRegex(ValueError,'quantity'):
            quote_fill(q,buying=True,quantity=65,now=now,settings=self.settings)

    def test_ema_signal_uses_completed_data_and_warmup(self):
        rows=[dict(date=str(date(2026,1,1)+timedelta(days=i)),open=100+i,high=102+i,
                   low=99+i,close=101+i) for i in range(60)]
        result=trend(rows)
        self.assertTrue(result['bullish'])
        self.assertFalse(result['cross_up'])
        with self.assertRaises(ValueError):
            trend(rows[:50])

class PaperJournalTests(unittest.TestCase):
    def test_read_only_check_never_writes_or_calls_broker(self):
        from unittest.mock import Mock, patch
        from nifty_daily_ema_monthly_paper import paper_step
        db, kite = Mock(), Mock()
        db.scalars.return_value=[]
        s=state(up=True)
        s['close']=24000
        now=datetime(2026,10,26,9,17,tzinfo=IST)
        with patch('nifty_daily_ema_monthly_paper.trend',return_value=s):
            result=paper_step(db,kite,1,[],[],now.date(),now,Settings(Decimal('30000')),weekday,check=True)
        self.assertEqual('CE',result['side'])
        self.assertFalse(result['real_orders_enabled'])
        db.add.assert_not_called()
        db.execute.assert_not_called()
        self.assertEqual([],kite.mock_calls)

    def test_bearish_exit_records_pnl_without_opening_puts(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from nifty_daily_ema_monthly_paper import paper_step
        db,kite=Mock(),Mock()
        position=SimpleNamespace(id=5,side='CE',option_expiry=date(2026,11,24),mode='paper',status='open',
                                 option_symbol='OLD_CALL',quantity=65,entry_premium=Decimal('500'),entry_index=Decimal('24100'))
        db.scalars.return_value=[position]
        db.scalar.return_value=None
        db.execute.return_value.all.return_value=[(k,'YES') for k in ('initial_stop','current_stop','target')]
        now=datetime(2026,10,26,9,17,tzinfo=IST)
        def quotes(symbols):
            symbol=symbols[0]
            if symbol=='NSE:NIFTY 50':
                return {symbol:dict(timestamp=now,last_price=24000)}
            return {symbol:dict(timestamp=now,depth={'buy':[{'price':400,'quantity':65}],
                                                     'sell':[{'price':450,'quantity':65}]})}
        kite.quote.side_effect=quotes
        s=state(bullish=False,down=True)
        s['close']=24000
        with patch('nifty_daily_ema_monthly_paper.trend',return_value=s):
            result=paper_step(db,kite,1,[],instruments(),now.date(),now,Settings(Decimal('30000')),weekday)
        self.assertEqual('exit',result['signal'])
        self.assertIsNone(result['side'])
        self.assertEqual('bearish',result['trend'])
        self.assertEqual(Decimal('-6500'),position.gross_option_pnl)
        self.assertEqual('closed',position.status)
        from trading.strategy_records import StrategyTrade
        trades=[c.args[0] for c in db.add.call_args_list if isinstance(c.args[0],StrategyTrade)]
        self.assertEqual([],trades)
        kite.place_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
