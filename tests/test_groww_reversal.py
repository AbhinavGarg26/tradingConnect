import logging
import unittest
from unittest.mock import Mock
from market.groww_reversal import GrowwReversalAdapter, GrowwReversalEntries, GrowwReversalExits
from market.reversal import ReversalWorker
from tests.test_reversal import MemoryConnection


SYMBOL = 'NIFTY26NOV25000CE'


class GrowwAdapterTests(unittest.TestCase):
    def setUp(self):
        self.kite = Mock()
        self.kite.instruments.return_value = [dict(exchange='NFO', tradingsymbol=SYMBOL, instrument_token=123)]
        self.kite.ltp.return_value = {f'NFO:{SYMBOL}': {'last_price': 110}}
        self.adapter = GrowwReversalAdapter(self.kite, 'token', logging.getLogger('test'), 1)
        self.adapter.client = Mock()
        self.order = dict(groww_order_id='g1', trading_symbol=SYMBOL, exchange='NSE', product='NRML',
            transaction_type='BUY', order_status='EXECUTED', filled_quantity=100, average_fill_price=100)
        self.row = dict(trading_symbol=SYMBOL, exchange='NSE', product='NRML', quantity=100,
                        realised_pnl=-50, net_price=100)
        self.adapter.client._get.side_effect = self.response

    def response(self, path, params):
        if path == '/order/list':
            return {'order_list': [self.order]}
        if path == '/positions/user':
            return {'positions': [self.row]}
        raise AssertionError(path)

    def test_native_positions_and_execution_entry_are_normalized(self):
        p = self.adapter.positions()['net'][0]
        self.assertEqual((p['exchange'], p['instrument_token'], p['pnl']), ('NFO', 123, 950))
        self.assertEqual(GrowwReversalEntries(self.adapter).resolve(self.adapter, p), (100, False))
        self.assertEqual(p['buy_value'], 10000)
        self.kite.positions.assert_not_called()
        self.kite.orders.assert_not_called()

    def test_mismatched_contract_and_missing_risk_field_fail_closed(self):
        self.kite.instruments.return_value = []
        with self.assertRaises(RuntimeError):
            self.adapter.positions()
        self.adapter._day = None
        self.kite.instruments.return_value = [dict(exchange='NFO', tradingsymbol=SYMBOL, instrument_token=123)]
        del self.row['realised_pnl']
        with self.assertRaises(KeyError):
            self.adapter.positions()

    def test_groww_funds_and_margin_do_not_use_kite_account(self):
        self.adapter.client._get.side_effect = None
        self.adapter.client._get.return_value = {'fno_margin_details': {'option_buy_balance_available': 12345}}
        self.assertEqual(self.adapter.margins('equity')['net'], 12345)
        self.adapter.client._request.return_value = {'total_requirement': 5250}
        order = dict(exchange='NFO', tradingsymbol=SYMBOL, quantity=50, product='NRML',
                     transaction_type='BUY', order_type='LIMIT', price=100)
        self.assertEqual(self.adapter.order_margins([order]), [{'total':5250}])
        args = self.adapter.client._request.call_args.args
        self.assertEqual(args[1], '/margins/detail/orders?segment=FNO')
        self.assertEqual(args[2][0]['exchange'], 'NSE')
        self.kite.margins.assert_not_called()

    def test_buy_payload_and_pending_partial_cancellation(self):
        self.adapter.client._request.return_value = {'groww_order_id': 'BUY1'}
        self.assertEqual(self.adapter.place_order(exchange='NFO', tradingsymbol=SYMBOL, quantity=50,
            product='NRML', transaction_type='BUY', order_type='LIMIT', price=100, tag='RVG123456', validity='IOC'), 'BUY1')
        body = self.adapter.client._request.call_args.args[2]
        self.assertEqual((body['segment'], body['validity'], body['order_reference_id']), ('FNO','DAY','RVG123456'))
        self.order.update(order_status='OPEN', order_reference_id='RVG123456', filled_quantity=25)
        normalized = self.adapter.orders()[0]
        self.assertEqual(normalized['filled_quantity'], 25)
        self.adapter.client._request.assert_called_with('POST', '/order/cancel', {'segment':'FNO', 'groww_order_id':'g1'})
        self.kite.place_order.assert_not_called()

    def test_order_pagination_and_terminal_status_mapping(self):
        self.adapter.client._get.side_effect = lambda path, params: {'order_list': [dict(self.order, groww_order_id=str(i)) for i in range(100)] if params['page']==0 else [dict(self.order,groww_order_id='last',order_status='EXPIRED')]}
        rows = self.adapter.orders()
        self.assertEqual(len(rows), 101)
        self.assertEqual(rows[-1]['status'], 'CANCELLED')
        self.assertEqual(self.adapter.client._get.call_count, 2)

    def test_smart_exit_cancellation_waits_for_broker_confirmation(self):
        self.adapter.client._get.side_effect = lambda path, params: {'orders':[{'smart_order_id':'gtt1'}]} if path.endswith('/list') else dict(trading_symbol=SYMBOL, exchange='NSE', product_type='NRML')
        exits = GrowwReversalExits(self.adapter)
        self.assertFalse(exits.remove_legacy_gtts(dict(tradingsymbol=SYMBOL, product='NRML')))
        self.adapter.client._request.assert_called_with('POST','/order-advance/cancel/FNO/GTT/gtt1',{})

    def test_pending_intent_cannot_be_claimed_by_other_broker(self):
        from datetime import datetime
        from unittest.mock import patch
        from market.reversal import IST
        worker = ReversalWorker(self.kite, 1, logging.getLogger('test'))
        state = {'count': 1, 'pending': {'broker':'groww', 'status':'BUY_SUBMITTING'}}
        with patch('market.reversal.datetime') as clock:
            clock.now.return_value = datetime(2026,11,17,11,0,tzinfo=IST)
            clock.strptime = datetime.strptime
            worker.cycle(MemoryConnection(state), {}, Mock(), set())
        self.kite.positions.assert_not_called()
        self.kite.place_order.assert_not_called()
