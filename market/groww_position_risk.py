"""Hard P&L exits for Groww long positions."""

from __future__ import annotations

import time
import uuid

from market.position_stops import HARD_STOP_LOSS_PCT, PROFIT_TARGET_PCT
from market.recovery_gtt_executor import GrowwRecoveryGttExecutor


TERMINAL_ORDER_STATUSES = {
    "COMPLETED", "COMPLETE", "CANCELLED", "REJECTED", "FAILED", "EXPIRED",
}


def hard_exit_reason(entry_price: float, ltp: float) -> str | None:
    if entry_price <= 0 or ltp <= 0:
        return None
    pnl_pct = ((ltp - entry_price) / entry_price) * 100
    if pnl_pct <= -HARD_STOP_LOSS_PCT:
        return "HARD_STOP_12PCT"
    if pnl_pct >= PROFIT_TARGET_PCT:
        return "PROFIT_TARGET_15PCT"
    return None


class GrowwMarketExitExecutor(GrowwRecoveryGttExecutor):
    """Cancel conflicting sells, then submit one market exit per position."""

    def __init__(self, access_token: str, logger, user_id: int):
        super().__init__(access_token, logger, user_id)
        self._submitted: set[str] = set()

    @staticmethod
    def _position_key(position: dict) -> str:
        return ":".join((
            str(position.get("segment", "")),
            str(position.get("exchange", "")),
            str(position.get("tradingsymbol", "")),
            str(position.get("product", "")),
        ))

    def remove_missing(self, active_keys: set[str]) -> None:
        self._submitted.intersection_update(active_keys)

    def exit_position(self, position: dict, reason: str) -> str | None:
        key = self._position_key(position)
        symbol = str(position["tradingsymbol"])
        segment = str(position["segment"])
        if key in self._submitted:
            return None

        order_payload = self._get(
            "/order/list", {"segment": segment, "page": 0, "page_size": 100}
        )
        orders = order_payload.get("order_list", [])
        conflicts = [
            order for order in orders
            if str(order.get("trading_symbol", "")).upper() == symbol.upper()
            and str(order.get("exchange", "")).upper()
                == str(position["exchange"]).upper()
            and str(order.get("product", "")).upper()
                == str(position["product"]).upper()
            and str(order.get("transaction_type", "")).upper() == "SELL"
            and str(order.get("order_status", "")).upper()
                not in TERMINAL_ORDER_STATUSES
        ]
        if conflicts:
            for order in conflicts:
                self._request("POST", "/order/cancel", {
                    "segment": segment,
                    "groww_order_id": order["groww_order_id"],
                })
                self.logger.warning(
                    "[%s] Groww cancellation requested for conflicting order %s",
                    symbol, order["groww_order_id"],
                )
            # Re-read broker state on the next cycle before placing the exit.
            return None

        reference_id = f"MBX{uuid.uuid4().hex[:17]}"
        response = self._request("POST", "/order/create", {
            "trading_symbol": symbol,
            "quantity": int(position["quantity"]),
            "validity": "DAY",
            "exchange": position["exchange"],
            "segment": segment,
            "product": position["product"],
            "order_type": "MARKET",
            "transaction_type": "SELL",
            "order_reference_id": reference_id,
        })
        order_id = str(response["groww_order_id"])
        self._submitted.add(key)
        self.logger.critical(
            "[%s] GROWW MARKET EXIT submitted: order=%s qty=%s reason=%s",
            symbol, order_id, position["quantity"], reason,
        )
        return order_id


class GrowwPositionRiskMonitor:
    """Poll Groww positions and enforce the same -12%/+15% exits as Kite."""

    def __init__(self, logger, user_id: int, interval_seconds: float = 1.0):
        self.logger = logger
        self.user_id = user_id
        self.interval_seconds = interval_seconds
        self._last_run = 0.0
        self._executor: GrowwMarketExitExecutor | None = None
        self._token: str | None = None

    def run_if_due(self, db, monotonic_now: float | None = None) -> None:
        now = time.monotonic() if monotonic_now is None else monotonic_now
        if now - self._last_run < self.interval_seconds:
            return
        self._last_run = now
        try:
            from trading.exchange_link import ExchangeLinkRepo

            link = ExchangeLinkRepo.get_for_user(db, self.user_id, provider="groww")
            if not link or not link.is_session_valid:
                return
            token = link.decrypt_session_token(db)
            if not token:
                return
            if self._executor is None or token != self._token:
                self._executor = GrowwMarketExitExecutor(token, self.logger, self.user_id)
                self._token = token

            positions: list[dict] = []
            for segment in ("CASH", "FNO"):
                payload = self._executor._get("/positions/user", {"segment": segment})
                positions.extend(payload.get("positions", []))

            active_keys: set[str] = set()
            for row in positions:
                quantity = int(row.get("quantity") or row.get("net_quantity") or 0)
                if quantity <= 0:
                    continue
                symbol = row.get("trading_symbol") or row.get("symbol")
                entry = float(
                    row.get("net_price")
                    or row.get("average_price")
                    or row.get("average_buy_price")
                    or row.get("buy_average_price")
                    or 0
                )
                if not symbol or entry <= 0:
                    continue
                position = {
                    "exchange": str(row.get("exchange") or "NSE").upper(),
                    "segment": str(row.get("segment") or "FNO").upper(),
                    "tradingsymbol": str(symbol),
                    "product": row.get("product") or row.get("product_type")
                        or ("NRML" if str(row.get("segment")).upper() == "FNO" else "CNC"),
                    "quantity": quantity,
                }
                active_keys.add(self._executor._position_key(position))
                ltp_payload = self._executor._get("/live-data/ltp", {
                    "segment": position["segment"],
                    "exchange_symbols": f'{position["exchange"]}_{symbol}',
                })
                ltp = float(ltp_payload.get(f'{position["exchange"]}_{symbol}') or 0)
                reason = hard_exit_reason(entry, ltp)
                if reason:
                    pnl_pct = ((ltp - entry) / entry) * 100
                    self.logger.critical(
                        "[%s] Groww hard boundary reached: entry=%.2f ltp=%.2f P&L=%.2f%%",
                        symbol, entry, ltp, pnl_pct,
                    )
                    self._executor.exit_position(position, reason)
            self._executor.remove_missing(active_keys)
        except Exception as exc:
            self.logger.exception("Groww position-risk check failed: %s", exc)
