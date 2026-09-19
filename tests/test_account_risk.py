from datetime import datetime
import unittest

from market.account_risk import (
    account_funds_snapshot,
    calculate_trade_stats,
    latest_loss_pair_signature,
    loss_level,
    performance_edge,
)


def closed(symbol, side, pnl, charges, order_id, minute):
    return {
        "symbol": symbol,
        "tradingsymbol": f"{symbol}26SEP25000{side}",
        "option_type": side,
        "status": "CLOSED",
        "realized_pnl": pnl,
        "total_charges": charges,
        "exit_order_id": order_id,
        "exit_time": datetime(2026, 9, 18, 10, minute),
    }


class AccountRiskTests(unittest.TestCase):
    def test_funds_use_opening_cash_plus_collateral_and_m2m_pnl(self):
        snapshot = account_funds_snapshot({
            "net": 75_000,
            "available": {
                "opening_balance": 100_000,
                "collateral": 20_000,
                "live_balance": 75_000,
            },
            "utilised": {"m2m_realised": -3_000, "m2m_unrealised": -3_000},
        })
        self.assertEqual(snapshot["total_funds"], 120_000)
        self.assertEqual(snapshot["current_pnl"], -6_000)
        self.assertEqual(snapshot["loss_pct"], 5.0)
        self.assertEqual(loss_level(snapshot["loss_pct"]), "soft")

    def test_four_and_eight_percent_loss_bands(self):
        self.assertEqual(loss_level(3.99), "normal")
        self.assertEqual(loss_level(4.0), "soft")
        self.assertEqual(loss_level(7.99), "soft")
        self.assertEqual(loss_level(8.0), "hard")

    def test_trade_stats_are_net_of_charges_and_split_by_symbol_side(self):
        rows = [
            closed("NIFTY", "CE", 1_000, 100, "1", 1),
            closed("NIFTY", "CE", 500, 100, "2", 2),
            closed("NIFTY", "CE", 300, 100, "3", 3),
            closed("NIFTY", "PE", -500, 100, "4", 4),
            closed("BANKNIFTY", "PE", -200, 100, "5", 5),
        ]
        stats = calculate_trade_stats(rows)
        self.assertEqual(stats["total_trades"], 5)
        self.assertEqual(stats["wins"], 3)
        self.assertEqual(stats["losses"], 2)
        self.assertEqual(stats["net_pnl"], 600)
        self.assertEqual(stats["ce"]["wins"], 3)
        self.assertEqual(stats["pe"]["losses"], 2)
        self.assertEqual(stats["symbol_sides"]["NIFTY CE"]["pnl"], 1_500)
        self.assertIn("Observed edge: CE", performance_edge(stats))

    def test_loss_pair_only_exists_when_latest_two_closed_trades_lost(self):
        rows = [
            closed("NIFTY", "CE", 500, 50, "WIN", 1),
            closed("NIFTY", "PE", -500, 50, "LOSS1", 2),
            closed("BANKNIFTY", "CE", -700, 50, "LOSS2", 3),
        ]
        self.assertEqual(latest_loss_pair_signature(rows), "LOSS1:LOSS2")
        rows.append(closed("NIFTY", "CE", 500, 50, "WIN2", 4))
        self.assertIsNone(latest_loss_pair_signature(rows))

    def test_edge_requires_at_least_three_trades_on_a_side(self):
        stats = calculate_trade_stats([
            closed("NIFTY", "CE", 1_000, 0, "1", 1),
            closed("NIFTY", "CE", 1_000, 0, "2", 2),
        ])
        self.assertIn("No reliable CE/PE edge", performance_edge(stats))


if __name__ == "__main__":
    unittest.main()
