import unittest
from datetime import datetime

from analytics.trade_reconciliation import (
    aggregate_order_executions,
    attach_order_charges,
    build_fifo_trade_rows,
    compare_open_quantities,
    signatures_match,
)


def execution(order_id, trade_id, side, quantity, price, timestamp, symbol="NIFTY26AUG25000CE"):
    return {
        "order_id": order_id,
        "trade_id": trade_id,
        "exchange": "NFO",
        "tradingsymbol": symbol,
        "product": "NRML",
        "transaction_type": side,
        "quantity": quantity,
        "average_price": price,
        "fill_timestamp": timestamp,
    }


class TradeReconciliationTests(unittest.TestCase):
    def test_signature_comparison_handles_open_and_closed_optional_values(self):
        base = {
            "tradingsymbol": "NIFTY26AUG25000CE",
            "trade_type": "BUY",
            "quantity": 65,
            "entry_price": 100.0,
            "total_charges": 20.0,
            "entry_order_id": "BUY1",
            "entry_time": datetime(2026, 8, 31, 9, 20),
        }
        open_row = {
            **base, "exit_price": None, "realized_pnl": None, "status": "OPEN",
            "exit_order_id": None, "exit_time": None,
        }
        closed_row = {
            **base, "exit_price": 105.0, "realized_pnl": 325.0, "status": "CLOSED",
            "exit_order_id": "SELL1",
            "exit_time": datetime(2026, 8, 31, 10, 20),
        }

        self.assertTrue(signatures_match([open_row, closed_row], [closed_row, open_row]))

    def test_partial_fills_of_same_order_are_not_lost(self):
        executions = [
            execution("BUY1", "T1", "BUY", 40, 100, "2026-08-31 09:20:00"),
            execution("BUY1", "T2", "BUY", 60, 102, "2026-08-31 09:20:01"),
        ]

        aggregated = aggregate_order_executions(executions)

        self.assertEqual(len(aggregated), 1)
        self.assertEqual(aggregated[0]["quantity"], 100)
        self.assertEqual(aggregated[0]["price"], 101.2)

    def test_partial_exit_leaves_only_true_residual_open(self):
        executions = aggregate_order_executions([
            execution("BUY1", "T1", "BUY", 100, 100, "2026-08-31 09:20:00"),
            execution("SELL1", "T2", "SELL", 40, 110, "2026-08-31 10:20:00"),
        ])

        rows = build_fifo_trade_rows(executions)
        closed = [row for row in rows if row["status"] == "CLOSED"]
        opened = [row for row in rows if row["status"] == "OPEN"]

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["quantity"], 40)
        self.assertEqual(closed[0]["realized_pnl"], 400)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["quantity"], 60)

    def test_complete_exit_has_no_minor_open_residual(self):
        executions = aggregate_order_executions([
            execution("BUY1", "T1", "BUY", 40, 100, "2026-08-31 09:20:00"),
            execution("BUY1", "T2", "BUY", 60, 102, "2026-08-31 09:20:01"),
            execution("SELL1", "T3", "SELL", 25, 110, "2026-08-31 10:20:00"),
            execution("SELL1", "T4", "SELL", 75, 108, "2026-08-31 10:20:02"),
        ])

        rows = build_fifo_trade_rows(executions)

        self.assertFalse(any(row["status"] == "OPEN" for row in rows))
        self.assertEqual(sum(row["quantity"] for row in rows), 100)

    def test_open_quantity_audit_finds_stale_database_residual(self):
        mismatches = compare_open_quantities(
            {"NIFTY26AUG25000CE": 5, "NIFTY26AUG25100CE": 50},
            {"NIFTY26AUG25000CE": 0, "NIFTY26AUG25100CE": 50},
        )

        self.assertEqual(mismatches, {"NIFTY26AUG25000CE": (5, 0)})

    def test_closed_trade_contains_proportional_buy_and_sell_total_charges(self):
        executions = aggregate_order_executions([
            execution("BUY1", "T1", "BUY", 100, 100, "2026-08-31 09:20:00"),
            execution("SELL1", "T2", "SELL", 40, 110, "2026-08-31 10:20:00"),
        ])
        executions[0]["total_charges"] = 30.0
        executions[1]["total_charges"] = 25.0

        rows = build_fifo_trade_rows(executions)
        closed = next(row for row in rows if row["status"] == "CLOSED")
        opened = next(row for row in rows if row["status"] == "OPEN")

        # 40% of ₹30 entry charges + all ₹25 exit charges.
        self.assertEqual(closed["total_charges"], 37.0)
        # Remaining 60% of the entry charges stays with the open quantity.
        self.assertEqual(opened["total_charges"], 18.0)

    def test_total_charges_are_loaded_by_parent_order_id(self):
        class ChargeKite:
            def __init__(self):
                self._routes = {}
                self.payload = None

            def _post(self, route, params, is_json):
                self.payload = params
                return [{"charges": {"brokerage": 17.5, "total": 23.75}}]

        kite = ChargeKite()
        orders = aggregate_order_executions([
            execution("UNIQUE_BUY_99", "T1", "BUY", 50, 100, "2026-08-31 09:20:00")
        ])

        success = attach_order_charges(
            kite,
            orders,
            [{"order_id": "UNIQUE_BUY_99", "variety": "regular", "order_type": "LIMIT"}],
        )

        self.assertTrue(success)
        self.assertEqual(orders[0]["total_charges"], 23.75)
        self.assertEqual(kite.payload[0]["order_id"], "UNIQUE_BUY_99")


if __name__ == "__main__":
    unittest.main()
