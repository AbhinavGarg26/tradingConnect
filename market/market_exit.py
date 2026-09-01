"""Idempotent market-order exits for the position monitor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from decimal import Decimal, ROUND_CEILING

from market.kite_orders import place_protected_market_order


EXIT_TAG_PREFIX = "MB"
MARKET_EXIT_TAG_PREFIX = "MBX"
LIMIT_EXIT_TAG_PREFIX = "MBL"
TERMINAL_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}
MAX_EXIT_ATTEMPTS = 3
EXIT_RETRY_DELAY = timedelta(seconds=2)
PROFIT_LIMIT_WAIT = timedelta(seconds=2)
OPTION_TICK_SIZE = Decimal("0.05")


class MarketExitExecutor:
    def __init__(self, kite, logger: logging.Logger):
        self.kite = kite
        self.logger = logger
        self._legacy_gtt_cleaned: set[str] = set()
        self._submitted_orders: dict[str, str] = {}
        self._attempts: dict[str, int] = {}
        self._last_attempt_at: dict[str, datetime] = {}
        self._cancel_requested: set[str] = set()
        self._submitted_at: dict[str, datetime] = {}
        self._submitted_order_types: dict[str, str] = {}
        self._market_fallback_required: set[str] = set()

    def remove_missing(self, active_keys: set[str]) -> None:
        """Forget exit ownership only after the broker no longer reports a position."""
        for key in set(self._submitted_orders) - active_keys:
            self._submitted_orders.pop(key, None)
            self._legacy_gtt_cleaned.discard(key)
            self._attempts.pop(key, None)
            self._last_attempt_at.pop(key, None)
            self._submitted_at.pop(key, None)
            self._submitted_order_types.pop(key, None)
            self._market_fallback_required.discard(key)

    def reset_position(self, position_key: str) -> None:
        """Clear order ownership when the same symbol starts a new entry lifecycle."""
        self._submitted_orders.pop(position_key, None)
        self._attempts.pop(position_key, None)
        self._last_attempt_at.pop(position_key, None)
        self._submitted_at.pop(position_key, None)
        self._submitted_order_types.pop(position_key, None)
        self._market_fallback_required.discard(position_key)
        self._cancel_requested.clear()

    def remove_legacy_gtts(self, position: dict) -> bool:
        """Delete old active GTTs before this process assumes exit ownership."""
        key = self._position_key(position)
        if key in self._legacy_gtt_cleaned:
            return True
        symbol = str(position["tradingsymbol"]).strip().upper()
        exchange = str(position["exchange"]).strip().upper()
        product = str(position["product"]).strip().upper()
        try:
            for gtt in self.kite.get_gtts():
                condition = gtt.get("condition", {})
                orders = gtt.get("orders", [])
                products = {str(order.get("product", "")).strip().upper() for order in orders}
                if (
                    gtt.get("status") == "active"
                    and str(condition.get("tradingsymbol", "")).strip().upper() == symbol
                    and str(condition.get("exchange", "")).strip().upper() == exchange
                    and (not products or product in products)
                ):
                    self.kite.delete_gtt(gtt["id"])
                    self.logger.warning("[%s] Deleted legacy GTT %s", symbol, gtt["id"])
            self._legacy_gtt_cleaned.add(key)
            return True
        except Exception as exc:
            self.logger.critical("[%s] Cannot remove legacy GTT: %s", symbol, exc)
            return False

    def exit_position(
        self,
        position: dict,
        reason: str,
        reference_price: float | None = None,
    ) -> str | None:
        """Place at most one live exit order for a position and return its ID."""
        symbol = position["tradingsymbol"]
        exchange = position["exchange"]
        product = position["product"]
        position_key = self._position_key(position)
        try:
            now = datetime.now(timezone.utc)
            orders = self.kite.orders()
            owned_order_id = self._submitted_orders.get(position_key)
            if owned_order_id:
                owned = next((o for o in orders if o.get("order_id") == owned_order_id), None)
                if owned and owned.get("status") in {"REJECTED", "CANCELLED"}:
                    self.logger.critical(
                        "[%s] Exit order %s ended as %s: %s",
                        symbol, owned_order_id, owned.get("status"), owned.get("status_message"),
                    )
                    submitted_type = self._submitted_order_types.pop(position_key, None)
                    self._submitted_orders.pop(position_key, None)
                    self._submitted_at.pop(position_key, None)
                    if submitted_type == self.kite.ORDER_TYPE_LIMIT:
                        self._market_fallback_required.add(position_key)
                elif (
                    owned
                    and self._submitted_order_types.get(position_key) == self.kite.ORDER_TYPE_LIMIT
                    and now - self._submitted_at.get(position_key, now) >= PROFIT_LIMIT_WAIT
                ):
                    if owned_order_id not in self._cancel_requested:
                        self.kite.cancel_order(
                            variety=owned.get("variety") or self.kite.VARIETY_REGULAR,
                            order_id=owned_order_id,
                            parent_order_id=owned.get("parent_order_id"),
                        )
                        self._cancel_requested.add(owned_order_id)
                        self._market_fallback_required.add(position_key)
                        self.logger.warning(
                            "[%s] Profit LIMIT %s not filled in %.1fs; cancellation requested",
                            symbol, owned_order_id, PROFIT_LIMIT_WAIT.total_seconds(),
                        )
                    return owned_order_id
                else:
                    self.logger.warning(
                        "[%s] Exit order %s already submitted (status=%s)",
                        symbol, owned_order_id, owned.get("status") if owned else "AWAITING_UPDATE",
                    )
                    return owned_order_id

            attempts = self._attempts.get(position_key, 0)
            last_attempt = self._last_attempt_at.get(position_key)
            if attempts >= MAX_EXIT_ATTEMPTS:
                self.logger.critical(
                    "[%s] Maximum market-exit attempts reached; MANUAL EXIT REQUIRED",
                    symbol,
                )
                return None
            if last_attempt and now - last_attempt < EXIT_RETRY_DELAY:
                return None

            matching = [
                order for order in orders
                if order.get("tradingsymbol") == symbol
                and order.get("exchange") == exchange
                and order.get("product") == product
                and order.get("transaction_type") == self.kite.TRANSACTION_TYPE_SELL
                and str(order.get("tag") or "").startswith(EXIT_TAG_PREFIX)
            ]
            pending = [o for o in matching if o.get("status") not in TERMINAL_STATUSES]
            if pending:
                order = pending[-1]
                self.logger.warning(
                    "[%s] Exit order %s still %s; not placing a duplicate",
                    symbol, order.get("order_id"), order.get("status"),
                )
                return order.get("order_id")

            if not self._conflicting_orders_are_cancelled(position, orders):
                return None

            remaining = self._remaining_quantity(position)
            if remaining <= 0:
                return None

            use_profit_limit = (
                self._is_soft_profit_exit(reason)
                and position_key not in self._market_fallback_required
                and reference_price is not None
                and reference_price > 0
            )
            if use_profit_limit:
                limit_price = self._one_tick_above(reference_price)
                tag = f"{LIMIT_EXIT_TAG_PREFIX}{int(position['instrument_token'])}{datetime.now():%H%M%S}"[:20]
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=symbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=remaining,
                    product=product,
                    order_type=self.kite.ORDER_TYPE_LIMIT,
                    price=limit_price,
                    validity=self.kite.VALIDITY_DAY,
                    tag=tag,
                )
                self._submitted_orders[position_key] = order_id
                self._submitted_at[position_key] = now
                self._submitted_order_types[position_key] = self.kite.ORDER_TYPE_LIMIT
                self.logger.critical(
                    "[%s] PROFIT LIMIT submitted: order=%s qty=%s price=₹%.2f "
                    "wait=%.1fs reason=%s",
                    symbol, order_id, remaining, limit_price,
                    PROFIT_LIMIT_WAIT.total_seconds(), reason,
                )
                return order_id

            tag = f"{MARKET_EXIT_TAG_PREFIX}{int(position['instrument_token'])}{datetime.now():%H%M%S}"[:20]
            self._attempts[position_key] = attempts + 1
            self._last_attempt_at[position_key] = now
            order_id = place_protected_market_order(
                self.kite,
                variety=self.kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=symbol,
                transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                quantity=remaining,
                product=product,
                order_type=self.kite.ORDER_TYPE_MARKET,
                validity=self.kite.VALIDITY_DAY,
                tag=tag,
            )
            self._submitted_orders[position_key] = order_id
            self._submitted_at[position_key] = now
            self._submitted_order_types[position_key] = self.kite.ORDER_TYPE_MARKET
            self._market_fallback_required.discard(position_key)
            self.logger.critical(
                "[%s] MARKET EXIT submitted: order=%s qty=%s reason=%s",
                symbol, order_id, remaining, reason,
            )
            return order_id
        except Exception as exc:
            self.logger.exception("[%s] MARKET EXIT FAILED (%s): %s", symbol, reason, exc)
            return None

    @staticmethod
    def _is_soft_profit_exit(reason: str) -> bool:
        return (
            reason.startswith(("PRE_PROFIT_", "PROFIT_"))
            and "HARD_FLOOR" not in reason
        )

    @staticmethod
    def _one_tick_above(reference_price: float) -> float:
        price = Decimal(str(reference_price)) + OPTION_TICK_SIZE
        ticks = (price / OPTION_TICK_SIZE).to_integral_value(rounding=ROUND_CEILING)
        return float(ticks * OPTION_TICK_SIZE)

    def _conflicting_orders_are_cancelled(self, position: dict, orders: list[dict]) -> bool:
        """Cancel same-position pending orders and wait for terminal confirmation."""
        symbol = position["tradingsymbol"]
        exchange = position["exchange"]
        product = position["product"]
        conflicts = [
            order for order in orders
            if order.get("tradingsymbol") == symbol
            and order.get("exchange") == exchange
            and order.get("product") == product
            and order.get("status") not in TERMINAL_STATUSES
            and not str(order.get("tag") or "").startswith(EXIT_TAG_PREFIX)
        ]
        if not conflicts:
            return True

        for order in conflicts:
            order_id = str(order["order_id"])
            if order_id in self._cancel_requested:
                self.logger.warning(
                    "[%s] Waiting for cancellation of order %s (status=%s)",
                    symbol, order_id, order.get("status"),
                )
                continue
            self.kite.cancel_order(
                variety=order.get("variety") or self.kite.VARIETY_REGULAR,
                order_id=order_id,
                parent_order_id=order.get("parent_order_id"),
            )
            self._cancel_requested.add(order_id)
            self.logger.warning(
                "[%s] Cancellation requested for conflicting %s order %s",
                symbol, order.get("order_type"), order_id,
            )
        return False

    def _remaining_quantity(self, original_position: dict) -> int:
        for position in self.kite.positions().get("net", []):
            if self._position_key(position) == self._position_key(original_position):
                return max(int(position.get("quantity", 0)), 0)
        return 0

    @staticmethod
    def _position_key(position: dict) -> str:
        return ":".join([
            str(position.get("exchange", "")),
            str(position.get("tradingsymbol", "")),
            str(position.get("product", "")),
        ])
