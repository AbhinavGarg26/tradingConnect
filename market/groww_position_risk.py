"""Hard P&L exits for Groww long positions."""

from __future__ import annotations

import time
import uuid
import threading
from functools import wraps

from market.position_stops import (
    DEFAULT_HARD_STOP_LOSS_PCT,
    hard_loss_reason,
    position_pnl_amount,
    position_pnl_pct,
)
from market.groww_entry_price import execution_entry_price
from market.recovery_gtt_executor import GrowwRecoveryGttExecutor
from market.kite_ltp import live_prices
from market.overnight_protection import carried_entry_price, overnight_protection_active


TERMINAL_ORDER_STATUSES = {
    "COMPLETED", "COMPLETE", "EXECUTED", "CANCELLED", "REJECTED", "FAILED", "EXPIRED",
}
CANCELLING_ORDER_STATUSES = {"CANCELLATION_REQUESTED", "CANCEL_REQUESTED"}


def hard_exit_reason(
    entry_price: float,
    ltp: float,
    hard_stop_loss_pct: float = DEFAULT_HARD_STOP_LOSS_PCT,
    quantity: int | float = 1,
    max_loss_amount: float | None = None,
) -> str | None:
    pnl_pct = position_pnl_pct(entry_price, ltp, quantity)
    if pnl_pct is None:
        return None
    pnl_amount = position_pnl_amount(entry_price, ltp, quantity)
    return hard_loss_reason(
        pnl_pct, hard_stop_loss_pct, pnl_amount, max_loss_amount
    )


_GROWW_EXIT_LOCK = threading.RLock()


def _serialized_exit(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with _GROWW_EXIT_LOCK:
            return method(self, *args, **kwargs)
    return wrapped


class GrowwMarketExitExecutor(GrowwRecoveryGttExecutor):
    """Cancel a matching pending exit, then submit one market exit per position."""

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

    @_serialized_exit
    def exit_position(self, position: dict, reason: str) -> str | None:
        key = self._position_key(position)
        symbol = str(position["tradingsymbol"])
        segment = str(position["segment"])
        exit_action = "SELL" if int(position["quantity"]) > 0 else "BUY"
        if key in self._submitted:
            return None

        orders = []
        for page in range(501):
            rows = self._get('/order/list', {'segment': segment, 'page': page, 'page_size': 100})['order_list']
            orders.extend(rows)
            if len(rows) < 100:
                break
        else:
            raise RuntimeError('Incomplete Groww exit order history')
        conflicts = [
            order for order in orders
            if str(order.get("trading_symbol", "")).upper() == symbol.upper()
            and str(order.get("exchange", "")).upper()
                == str(position["exchange"]).upper()
            and str(order.get("product", "")).upper()
                == str(position["product"]).upper()
            and str(order.get("transaction_type", "")).upper() == exit_action
            and str(order.get("order_status", "")).upper()
                not in TERMINAL_ORDER_STATUSES
        ]
        if conflicts:
            owned = [o for o in conflicts if str(o.get('order_reference_id') or '').startswith('MBX')
                     and str(o.get('order_type') or '').upper() == 'MARKET']
            if owned:
                return str(owned[-1]['groww_order_id'])
            for order in conflicts:
                status = str(order.get("order_status", "")).upper()
                if status in CANCELLING_ORDER_STATUSES:
                    self.logger.warning(
                        "[%s] Waiting for conflicting order %s cancellation to complete",
                        symbol, order.get("groww_order_id"),
                    )
                    continue
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

        # Refresh under the same process-wide lock used by reversal exits.
        # A completed exit must not turn a stale long snapshot into a short.
        rows = self._get('/positions/user', {'segment': segment})['positions']
        matching = [r for r in rows if str(r.get('trading_symbol') or r.get('symbol')) == symbol
                    and str(r.get('exchange')) == str(position['exchange'])
                    and str(r.get('product') or r.get('product_type')) == str(position['product'])]
        remaining = sum(int(r.get('quantity') or r.get('net_quantity') or 0) for r in matching)
        if remaining == 0 or (remaining > 0) != (int(position['quantity']) > 0):
            return None
        quantity = min(abs(remaining), abs(int(position['quantity'])))
        reference_id = f"MBX{uuid.uuid4().hex[:17]}"
        response = self._request("POST", "/order/create", {
            "trading_symbol": symbol,
            "quantity": quantity,
            "validity": "DAY",
            "exchange": position["exchange"],
            "segment": segment,
            "product": position["product"],
            "order_type": "MARKET",
            "transaction_type": exit_action,
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
    """Poll Groww positions and enforce the configured hard-loss exit."""

    def __init__(self, logger, user_id: int, kite, interval_seconds: float = 1.0):
        self.logger = logger
        self.user_id = user_id
        self.kite = kite
        self.interval_seconds = interval_seconds
        self._last_run = 0.0
        self._executor: GrowwMarketExitExecutor | None = None
        self._token: str | None = None

    def run_if_due(
        self,
        db,
        monotonic_now: float | None = None,
        hard_stop_loss_pct: float = DEFAULT_HARD_STOP_LOSS_PCT,
        ignored_symbols: set[str] | None = None,
        max_loss_amount: float | None = None,
        stop_loss_override_symbols: set[str] | None = None,
        now=None,
    ) -> None:
        wall_clock_now = now
        monotonic_clock = time.monotonic() if monotonic_now is None else monotonic_now
        if monotonic_clock - self._last_run < self.interval_seconds:
            return
        self._last_run = monotonic_clock
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
            orders_by_segment: dict[str, list[dict]] = {}
            for segment in ("CASH", "FNO"):
                payload = self._executor._get("/positions/user", {"segment": segment})
                positions.extend(payload.get("positions", []))
                orders_by_segment[segment] = self._executor._get(
                    "/order/list", {"segment": segment, "page": 0, "page_size": 100}
                ).get("order_list", [])

            open_rows = [
                row for row in positions
                if int(row.get("quantity") or row.get("net_quantity") or 0) != 0
            ]
            # Groww's portfolio endpoint remains the source of positions, but
            # Kite is the single source of live LTPs for all broker monitors.
            # One batched request avoids Groww live-data permissions/rate errors.
            prices = live_prices(self.kite, open_rows)
            active_keys: set[str] = set()
            ignored = {str(symbol).strip().upper() for symbol in (ignored_symbols or set())}
            for row in open_rows:
                quantity = int(row.get("quantity") or row.get("net_quantity") or 0)
                symbol = row.get("trading_symbol") or row.get("symbol")
                broker_entry = float(
                    row.get("net_price")
                    or row.get("average_price")
                    or row.get("average_buy_price")
                    or row.get("buy_average_price")
                    or 0
                )
                if not symbol or broker_entry <= 0:
                    continue
                overnight = carried_entry_price(row) is not None
                if overnight and not overnight_protection_active(wall_clock_now):
                    self.logger.info("[%s] Carried-position stop protection begins at 09:30 IST", symbol)
                    continue
                position = {
                    "exchange": str(row.get("exchange") or "NSE").upper(),
                    "segment": str(row.get("segment") or "FNO").upper(),
                    "tradingsymbol": str(symbol),
                    "product": row.get("product") or row.get("product_type")
                        or ("NRML" if str(row.get("segment")).upper() == "FNO" else "CNC"),
                    "quantity": quantity,
                }
                # Keep ignored positions in the executor's active set. Ignoring
                # suppresses profit actions only; hard-loss protection remains.
                active_keys.add(self._executor._position_key(position))
                entry = execution_entry_price(
                    orders_by_segment.get(str(row.get("segment") or "FNO").upper(), []),
                    row,
                )
                if entry is None and overnight:
                    entry = carried_entry_price(row)
                if entry is None:
                    self.logger.error(
                        "[%s] Cannot reconcile Groww fills to open quantity; hard stop skipped (broker net_price=%.2f)",
                        symbol, broker_entry,
                    )
                    continue
                if abs(entry - broker_entry) >= 0.01:
                    self.logger.warning(
                        "[%s] Groww net_price %.2f differs from execution entry %.2f; using executions",
                        symbol, broker_entry, entry,
                    )
                ltp = prices.get((position["exchange"], position["segment"], position["tradingsymbol"]), 0)
                if ltp <= 0:
                    self.logger.warning("[%s] Kite LTP unavailable; Groww protection did not evaluate this cycle", symbol)
                    continue
                overridden = str(symbol).strip().upper() in (stop_loss_override_symbols or set())
                reason = None if overridden else hard_exit_reason(
                    entry, ltp, hard_stop_loss_pct, quantity, max_loss_amount
                )
                if overridden:
                    self.logger.warning(
                        "[%s] Temporary stop-loss override active; Groww software exit skipped", symbol
                    )
                if str(symbol).strip().upper() in ignored:
                    self.logger.info(
                        "[%s] Groww profit rules ignored; hard loss limits remain active",
                        symbol,
                    )
                if reason:
                    pnl_pct = position_pnl_pct(entry, ltp, quantity)
                    self.logger.critical(
                        "[%s] Groww hard boundary reached: entry=%.2f ltp=%.2f P&L=%.2f%%",
                        symbol, entry, ltp, pnl_pct,
                    )
                    self._executor.exit_position(position, reason)
            self._executor.remove_missing(active_keys)
        except Exception as exc:
            self.logger.exception("Groww position-risk check failed: %s", exc)
