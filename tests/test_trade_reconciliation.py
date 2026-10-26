import unittest
from datetime import datetime

from analytics.trade_reconciliation import (
    TradeReconciliationScheduler,
    _row_identity,
    aggregate_order_executions,
    attach_order_charges,
    build_fifo_trade_rows,
    compare_open_quantities,
    is_before_session_reset,
    reconcile_broker_day_pnl,
    repair_misattributed_carryover_closures,
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
    def test_periodic_reconciliation_defaults_to_ten_minutes(self):
        self.assertEqual(TradeReconciliationScheduler().interval_seconds, 600.0)

    def test_execution_identity_ignores_correctable_entry_price(self):
        row = {
            "entry_order_id": "ENTRY-1",
            "exit_order_id": "EXIT-1",
            "tradingsymbol": "NIFTY26OCT22600CE",
            "trade_type": "BUY",
            "quantity": 65,
            "status": "CLOSED",
            "entry_price": 200.5,
        }
        corrected = {**row, "entry_price": 201.25}

        self.assertEqual(_row_identity(row), _row_identity(corrected))

    def test_same_day_execution_corrects_stale_carryover_price_in_place(self):
        class Result:
            rowcount = 1

        class FakeSession:
            def __init__(self):
                self.params = None

            def execute(self, _statement, params):
                self.params = params
                return Result()

        db = FakeSession()
        repaired = repair_misattributed_carryover_closures(
            db,
            user_id=7,
            exchange_link_id="00000000-0000-0000-0000-000000000001",
            day_start=datetime(2026, 10, 26),
            expected_rows=[{
                "symbol": "NIFTY",
                "tradingsymbol": "NIFTY26OCT22500CE",
                "option_type": "CE",
                "entry_order_id": "BUY-TODAY",
                "exit_order_id": "SELL-TODAY",
                "trade_type": "BUY",
                "quantity": 65,
                "entry_price": 158.95,
                "exit_price": 142.6,
                "realized_pnl": -1062.75,
                "total_charges": 77.75,
                "status": "CLOSED",
                "entry_time": datetime(2026, 10, 26, 9, 10),
                "exit_time": datetime(2026, 10, 26, 9, 17),
            }],
        )

        self.assertEqual(repaired, {"NIFTY"})
        self.assertEqual(db.params["entry_price"], 158.95)
        self.assertEqual(db.params["entry_order_id"], "BUY-TODAY")

    def test_broker_pnl_audit_removes_legacy_adjustment_without_creating_trade(self):
        class Result:
            def __init__(self, value=None):
                self.value = value

            def scalar(self):
                return self.value

        class FakeSession:
            def __init__(self):
                self.statements = []

            def execute(self, statement, params=None):
                sql = str(statement)
                self.statements.append((sql, params or {}))
                if "SELECT id FROM market_trades" in sql:
                    return Result(42)
                if "DELETE FROM market_trades" in sql:
                    return Result()
                raise AssertionError(f"Unexpected statement: {sql}")

        db = FakeSession()
        repaired = reconcile_broker_day_pnl(
            db,
            user_id=7,
            exchange_link_id="00000000-0000-0000-0000-000000000001",
            day_start=datetime(2026, 10, 26),
            day_positions=[{
                "tradingsymbol": "NIFTY26OCT22500CE",
                "quantity": 0,
                # An overnight close has an explicit zero day buy.  This used
                # to fall back to carry-over totals and create a fake trade.
                "day_buy_quantity": 0,
                "day_sell_quantity": 65,
                "buy_quantity": 65,
                "sell_quantity": 65,
                "day_buy_value": 0,
                "day_sell_value": 9_269,
                "buy_value": 10_332,
                "sell_value": 9_269,
            }],
        )

        self.assertEqual(repaired, {"NIFTY"})
        self.assertTrue(any("DELETE FROM market_trades" in sql for sql, _ in db.statements))
        self.assertFalse(any("INSERT INTO market_trades" in sql for sql, _ in db.statements))

    def test_reconciliation_waits_for_the_six_am_session_reset(self):
        self.assertTrue(is_before_session_reset(datetime(2026, 9, 30, 5, 59)))
        self.assertFalse(is_before_session_reset(datetime(2026, 9, 30, 6, 0)))

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
