import logging
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock
from market.reversal import IST, ReversalWorker, affordable_quantity, confirmed


class ReversalTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 17, 11, 3, 1, tzinfo=IST)
        self.trigger = self.now.replace(minute=1, second=20)
        self.candles = [
            dict(date=self.now.replace(minute=1, second=0), high=103, low=100, close=101),
            dict(date=self.now.replace(minute=2, second=0), high=101, low=98, close=99),
        ]

    def test_ce_requires_closed_post_trigger_breakdown(self):
        self.assertTrue(confirmed('CE', self.candles, self.trigger, self.now))
        self.assertFalse(confirmed('PE', self.candles, self.trigger, self.now))
        self.assertFalse(confirmed('CE', self.candles, self.trigger, self.now-timedelta(seconds=10)))
        self.assertFalse(confirmed('CE', self.candles, self.now, self.now))
        self.assertFalse(confirmed('CE', self.candles, self.trigger, self.now+timedelta(minutes=3)))

    def test_pe_breakout_and_missing_minute(self):
        self.candles[-1]['close'] = 104
        self.assertTrue(confirmed('PE', self.candles, self.trigger, self.now))
        self.candles[0]['date'] -= timedelta(minutes=1)
        self.assertFalse(confirmed('PE', self.candles, self.trigger, self.now))

    def test_funds_reduce_whole_lots_and_risk_caps_quantity(self):
        self.assertEqual(affordable_quantity(200, 50, 100, 16000, 5, 5000, 10), 150)
        self.assertEqual(affordable_quantity(200, 50, 100, 100000, 5, 1100, 10), 100)
        self.assertEqual(affordable_quantity(50, 50, 100, 4999, 5, 5000, 10), 0)
        self.assertEqual(affordable_quantity(50, 50, float('nan'), 10000, 5, 5000, 10), 0)

    def test_unknown_buy_submission_is_never_retried(self):
        kite = Mock()
        kite.orders.return_value = []
        worker = ReversalWorker(kite, 1, logging.getLogger('test'))
        pending = dict(status='BUY_SUBMITTING', tag='RV1')
        worker.advance(pending, [], self.now, self.now.time(), 2000, {}, Mock(), set(), Mock())
        kite.place_order.assert_not_called()
        self.assertEqual(pending['status'], 'BUY_SUBMITTING')

    def test_partial_buy_is_not_topped_up(self):
        kite = Mock()
        kite.orders.return_value = [dict(tag='RV1', order_id='42', status='CANCELLED', filled_quantity=50)]
        worker = ReversalWorker(kite, 1, logging.getLogger('test'))
        pending = dict(status='BUY_SENT', tag='RV1')
        save = Mock()
        worker.advance(pending, [], self.now, self.now.time(), 2000, {}, Mock(), set(), save)
        self.assertEqual(pending['status'], 'DONE')
        kite.place_order.assert_not_called()
        save.assert_called_once()

    def test_flat_position_without_complete_owned_exit_cannot_buy(self):
        kite = Mock()
        kite.orders.return_value = [dict(order_id='e', status='OPEN', filled_quantity=25)]
        worker = ReversalWorker(kite, 1, logging.getLogger('test'))
        pending = dict(status='EXIT', trigger=self.trigger.isoformat(), exit_order_id='e',
                       position=dict(exchange='NFO', tradingsymbol='ABC', product='MIS', quantity=50))
        settings = Mock(ignored_symbols=set())
        worker.advance(pending, [], self.now, self.now.replace(hour=15).time(), 2000,
                       {'reversal_confirmation_window_seconds': 180}, settings, set(), Mock())
        self.assertEqual(pending['status'], 'SKIPPED')
        kite.place_order.assert_not_called()
        kite.historical_data.assert_not_called()

class MemoryConnection:
    def __init__(self, state):
        self.state = state
    def execute(self, sql, params):
        import json
        if str(sql).startswith('UPDATE'):
            self.state = json.loads(params['s'])
        return Mock(scalar=lambda: self.state)
    def commit(self):
        pass


class DurableCycleTests(unittest.TestCase):
    def cycle(self, state, price=108, cfg=None):
        from types import SimpleNamespace
        from unittest.mock import patch
        now = datetime(2026, 11, 17, 11, 3, 1, tzinfo=IST)
        kite = Mock()
        kite.positions.return_value = {'net': [dict(exchange='NFO', tradingsymbol='ABC26CE',
            instrument_token=1, product='MIS', quantity=50, buy_quantity=50, buy_value=5000, pnl=0)]}
        kite.quote.return_value = {'NFO:ABC26CE': dict(last_trade_time=now, last_price=price)}
        kite.instruments.return_value = [dict(instrument_token=1, instrument_type='CE', exchange='NFO')]
        worker = ReversalWorker(kite, 1, logging.getLogger('test'))
        worker.entries = Mock(resolve=Mock(return_value=(100, False)))
        # Keep real position key behavior for persistence lookup.
        from market.entry_price_tracker import CurrentEntryPriceTracker
        worker.entries.position_key = CurrentEntryPriceTracker.position_key
        conn = MemoryConnection(state)
        settings = SimpleNamespace(ignored_symbols=set(), hard_stop_loss_pct=10, max_loss_amount=None)
        with patch('market.reversal.datetime') as clock:
            clock.now.return_value = now
            clock.strptime = datetime.strptime
            clock.fromisoformat = datetime.fromisoformat
            worker.cycle(conn, cfg or {}, settings, set())
        kite.place_order.assert_not_called()
        return conn.state, kite

    def test_persisted_profit_ceiling_stays_blocked_after_price_falls(self):
        state = {'peaks': {'NFO:ABC26CE:MIS': dict(marker=[50,5000,100], peak=111, blocked=True)}}
        updated, _ = self.cycle(state, price=102)
        self.assertTrue(updated['peaks']['NFO:ABC26CE:MIS']['blocked'])
        self.assertEqual(updated['peaks']['NFO:ABC26CE:MIS']['peak'], 111)

    def test_daily_limit_survives_new_worker(self):
        _, kite = self.cycle({'count': 1})
        kite.quote.assert_not_called()

    def test_two_is_configurable_but_more_than_two_fails_closed(self):
        _, kite = self.cycle({'count': 1}, cfg={'reversal_max_per_day': 2})
        kite.quote.assert_called_once()
        _, kite = self.cycle({}, cfg={'reversal_max_per_day': 3})
        kite.positions.assert_not_called()

    def test_peak_is_persisted_before_drawdown(self):
        updated, _ = self.cycle({}, price=108)
        self.assertEqual(updated['peaks']['NFO:ABC26CE:MIS']['peak'], 108)

class ReversalBuyFlowTests(unittest.TestCase):
    def exercise(self, timeout=False):
        import copy
        from types import SimpleNamespace
        from unittest.mock import patch
        now = datetime(2026, 11, 17, 11, 3, 1, tzinfo=IST)
        broker = Mock()
        broker.orders.return_value = [dict(order_id='exit1', status='COMPLETE', filled_quantity=200, tradingsymbol='ABC26CE')]
        broker.positions.return_value = {'net': []}
        broker.historical_data.return_value = [
            dict(date=now.replace(minute=1,second=0), high=103, low=100, close=101),
            dict(date=now.replace(minute=2,second=0), high=101, low=98, close=99)]
        broker.quote.return_value = {'NFO:ABC26PE': dict(last_trade_time=now,
            depth={'sell':[{'price':100}], 'buy':[{'price':99}]})}
        broker.margins.return_value = {'net':16000, 'available':{'live_balance':16000}}
        # Broker margin forces another whole-lot reduction after local sizing.
        broker.order_margins.side_effect = lambda orders: [{'total': orders[0]['quantity']*110}]
        broker.place_order.return_value = 'buy1'
        if timeout:
            broker.place_order.side_effect = TimeoutError('uncertain outcome')
        worker = ReversalWorker(broker, 1, logging.getLogger('test'))
        pending = dict(status='EXIT', trigger=now.replace(minute=1,second=20).isoformat(),
            exit_order_id='exit1', entry=100, side='CE', underlying=123, target='ABC26PE',
            lot=50, tick=.05, tag='RV123456', position=dict(exchange='NFO', tradingsymbol='ABC26CE',
            product='MIS', quantity=200))
        cfg = dict(reversal_confirmation_window_seconds=180, reversal_funds_buffer_pct=5,
                   reversal_daily_loss_limit=2000, risk_per_trade=2000, max_open_trades=5)
        saves = []
        settings = SimpleNamespace(ignored_symbols=set(), hard_stop_loss_pct=10, max_loss_amount=None)
        with patch('market.reversal.datetime') as clock:
            clock.now.return_value = now
            clock.fromisoformat = datetime.fromisoformat
            if timeout:
                with self.assertRaises(TimeoutError):
                    worker.advance(pending, [], now, now.replace(hour=15).time(), 2000, cfg, settings, set(),
                                   lambda: saves.append(copy.deepcopy(pending)))
            else:
                worker.advance(pending, [], now, now.replace(hour=15).time(), 2000, cfg, settings, set(),
                               lambda: saves.append(copy.deepcopy(pending)))
        return broker, pending, saves

    def test_complete_exit_confirmation_and_margin_reduce_lots_before_buy(self):
        broker, pending, saves = self.exercise()
        self.assertEqual(broker.place_order.call_args.kwargs['quantity'], 100)
        self.assertEqual(broker.place_order.call_args.kwargs['validity'], 'IOC')
        self.assertEqual(saves[-2]['status'], 'BUY_SUBMITTING')
        self.assertEqual(pending['status'], 'BUY_SENT')
        self.assertEqual(broker.order_margins.call_count, 2)

    def test_timeout_keeps_durable_submission_intent(self):
        broker, pending, saves = self.exercise(timeout=True)
        self.assertEqual(saves[-1]['status'], 'BUY_SUBMITTING')
        self.assertEqual(pending['status'], 'BUY_SUBMITTING')
        broker.place_order.assert_called_once()
