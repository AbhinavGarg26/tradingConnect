import logging
import unittest

from market.groww_position_risk import GrowwMarketExitExecutor, hard_exit_reason


POSITION = {
    "exchange": "NSE",
    "segment": "FNO",
    "tradingsymbol": "NIFTY26OCT25000CE",
    "product": "NRML",
    "quantity": 65,
}


class FakeGrowwExit(GrowwMarketExitExecutor):
    def __init__(self, orders=None):
        super().__init__("token", logging.getLogger("test"), 1)
        self.orders = orders or []
        self.requests = []

    def _get(self, path, params):
        return {"order_list": self.orders}

    def _request(self, method, path, body):
        self.requests.append((method, path, body))
        if path == "/order/create":
            return {"groww_order_id": "G1", "order_status": "OPEN"}
        return {"groww_order_id": body["groww_order_id"], "order_status": "CANCELLED"}


class GrowwPositionRiskTests(unittest.TestCase):
    def test_hard_boundaries(self):
        self.assertIsNone(hard_exit_reason(100, 88.01))
        self.assertEqual(hard_exit_reason(100, 88), "HARD_STOP_12PCT")
        self.assertIsNone(hard_exit_reason(100, 115))
        self.assertEqual(hard_exit_reason(100, 91.5, 8.5), "HARD_STOP_8.5PCT")

    def test_market_sell_payload_and_deduplication(self):
        executor = FakeGrowwExit()
        self.assertEqual(executor.exit_position(POSITION, "HARD_STOP_12PCT"), "G1")
        self.assertIsNone(executor.exit_position(POSITION, "HARD_STOP_12PCT"))
        creates = [item for item in executor.requests if item[1] == "/order/create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][2]["order_type"], "MARKET")
        self.assertEqual(creates[0][2]["transaction_type"], "SELL")

    def test_conflicting_sell_is_cancelled_before_market_exit(self):
        executor = FakeGrowwExit([{
            "groww_order_id": "OLD1",
            "trading_symbol": POSITION["tradingsymbol"],
            "exchange": "NSE",
            "segment": "FNO",
            "product": "NRML",
            "transaction_type": "SELL",
            "order_status": "OPEN",
        }])
        self.assertIsNone(executor.exit_position(POSITION, "HARD_STOP_12PCT"))
        self.assertEqual(executor.requests[0][1], "/order/cancel")
        self.assertFalse(any(item[1] == "/order/create" for item in executor.requests))


if __name__ == "__main__":
    unittest.main()
