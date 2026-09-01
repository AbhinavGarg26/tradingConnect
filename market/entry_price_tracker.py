"""Resolve the true current open-leg entry price from Kite executions."""

from __future__ import annotations

from analytics.trade_reconciliation import aggregate_order_executions, build_fifo_trade_rows


class CurrentEntryPriceTracker:
    def __init__(self):
        self._cache: dict[str, tuple[tuple, float]] = {}

    @staticmethod
    def position_key(position: dict) -> str:
        return ":".join(str(position.get(key, "")) for key in (
            "exchange", "tradingsymbol", "product"
        ))

    @staticmethod
    def marker(position: dict) -> tuple:
        return (
            int(position.get("buy_quantity") or 0),
            round(float(position.get("buy_value") or 0), 4),
            int(position.get("sell_quantity") or 0),
            round(float(position.get("sell_value") or 0), 4),
            int(position.get("quantity") or 0),
        )

    def resolve(self, kite, position: dict) -> tuple[float, bool]:
        """Return FIFO open-leg average and whether broker executions changed."""
        key = self.position_key(position)
        marker = self.marker(position)
        cached = self._cache.get(key)
        if cached and cached[0] == marker:
            return cached[1], False

        rows = build_fifo_trade_rows(aggregate_order_executions(kite.trades()))
        matching = [
            row for row in rows
            if row["status"] == "OPEN"
            and row["trade_type"] == "BUY"
            and row["tradingsymbol"] == position["tradingsymbol"]
        ]
        total_qty = sum(int(row["quantity"]) for row in matching)
        expected_qty = int(position.get("quantity") or 0)
        if expected_qty <= 0 or total_qty != expected_qty:
            raise RuntimeError(
                f"execution ledger quantity {total_qty} != broker position {expected_qty}"
            )
        price = sum(float(row["entry_price"]) * int(row["quantity"]) for row in matching) / total_qty
        changed = cached is not None and cached[0] != marker
        self._cache[key] = (marker, round(price, 4))
        return round(price, 4), changed

    def remove_missing(self, active_keys: set[str]) -> None:
        for key in set(self._cache) - active_keys:
            self._cache.pop(key, None)
