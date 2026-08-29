"""
strategy_engine.py — orchestrates trailing SL evaluation on every candle close.

Called by WebSocketEngine via on_candle_close(candle, user_id).

Per closed candle it:
  1. Finds all open trades for that instrument
  2. Checks if SL is hit on current close
  3. Checks support rejection (with next-candle confirmation)
  4. Runs the configured trail method (or per-trade override)
  5. Emits OrderSignal → OrderExecutor (next module)
  6. Updates trade.current_sl + writes StopLossHistory to DB
  7. Sends Telegram alerts for significant events

Flow per candle:
  evaluate(candle, user_id)
      │
      ├── SL hit?          → emit EXIT signal
      ├── Support reject?  → emit EXIT signal (if confirmed)
      └── Trail?           → emit MODIFY_SL signal
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from datetime import timezone
from enum import Enum
from typing import Callable, List, Optional

from trading.alerts import Alerter
from trading.candle_builder import Candle
from trading.candle_store import CandleStore
from trading.database import get_db
from trading.models import Trade
from trading.repositories import MarketConfigRepo, SupportLevelRepo, TradeRepo, InstrumentRepo
from trading.trail_methods import (
    ATRTrail,
    EMATrail,
    RMultipleTrail,
    SupportRejectionDetector,
    SwingTrail,
    TrailResult,
)
from trading.utils import is_market_hours

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# OrderSignal — emitted to OrderExecutor
# ─────────────────────────────────────────────────────────────

class SignalType(Enum):
    EXIT       = "exit"        # close the trade at market
    MODIFY_SL  = "modify_sl"   # update SL order on Kite
    ALERT_ONLY = "alert_only"  # no order — just notify


@dataclass
class OrderSignal:
    signal_type:  SignalType
    trade:        Trade
    new_sl:       Optional[Decimal] = None   # for MODIFY_SL
    exit_reason:  Optional[str]     = None   # for EXIT
    trail_result: Optional[TrailResult] = None
    candle:       Optional[Candle]  = None


# ─────────────────────────────────────────────────────────────
# StrategyEngine
# ─────────────────────────────────────────────────────────────

class StrategyEngine:
    """
    Stateful engine — one instance per running session.
    Holds CandleStore and SupportRejectionDetector state in memory.

    Usage in main.py:
        engine = StrategyEngine(user_id=user_id)
        ws_engine.register_strategy_handler(engine.evaluate)
        # OrderExecutor wires in via:
        engine.on_signal(order_executor.handle)
    """

    def __init__(self, user_id: int):
        self.user_id    = user_id
        self._store     = CandleStore(maxlen=100)
        self._rejection = SupportRejectionDetector()
        self._alerter:  Optional[Alerter] = None
        self._config:   dict = {}
        self._signal_handlers: List[Callable[[OrderSignal], None]] = []
        self._initialised = False

    # ── Public API ────────────────────────────────────────────

    def on_signal(self, handler: Callable[[OrderSignal], None]) -> None:
        """Register a handler that receives every OrderSignal."""
        self._signal_handlers.append(handler)

    def evaluate(self, candle: Candle, user_id: uuid.UUID) -> None:
        if not is_market_hours():
            return

        if not self._initialised:
            self._load_config()

        self._store.push(candle)
        token = candle.instrument_token
        candles = self._store.get(token, candle.timeframe_minutes)

        if len(candles) < 2:
            return

        with get_db() as db:
            # ── Look up instrument by token first ──
            instrument = InstrumentRepo.get_by_token(db, token)
            if not instrument:
                logger.debug("No instrument found for token=%s", token)
                return

            open_trades = TradeRepo.get_open_by_instrument(db, instrument.id)
            if not open_trades:
                logger.debug("No open trades for instrument=%s", instrument.symbol)
                return

            logger.info(
                "Evaluating %d trade(s) for %s candle=%s close=%s",
                len(open_trades), instrument.symbol, candle.open_time, candle.close
            )

            for trade in open_trades:
                try:
                    self._evaluate_trade(db, trade, candle, candles)
                except Exception as exc:
                    logger.error(
                        "Strategy error trade_id=%s token=%s: %s",
                        trade.id, token, exc, exc_info=True,
                    )

    # ── Per-trade evaluation ──────────────────────────────────

    def _evaluate_trade(self, db, trade: Trade, candle: Candle, candles: list) -> None:
        # Skip candles before trade entry
        candle_time = candle.open_time
        if candle_time.tzinfo is None:
            candle_time = candle_time.replace(tzinfo=timezone.utc)

        trade_entry = trade.entered_at
        if trade_entry.tzinfo is None:
            trade_entry = trade_entry.replace(tzinfo=timezone.utc)

        if candle_time < trade_entry:
            return

        ltp          = Decimal(str(candle.close))
        prev_candle  = candles[-2]

        # ── 1. SL hit check ───────────────────────────────────
        if trade.is_sl_hit(ltp):
            logger.info(
                "SL hit — trade_id=%s symbol=%s sl=%.2f ltp=%.2f",
                trade.id, trade.instrument.symbol, trade.current_sl, ltp,
            )
            self._alerter.sl_hit(
                trade.instrument.symbol,
                float(trade.current_sl),
                float(ltp),
            )
            # Close the trade immediately in the SAME session
            trade.close(exit_price=ltp, exit_reason="sl_hit")
            db.flush()  # persists before session closes

            self._emit(OrderSignal(
                signal_type=SignalType.EXIT,
                trade=trade,
                exit_reason="sl_hit",
                candle=candle,
            ))
            self._rejection.clear_trade(trade.id)
            return

        # ── 2. Target hit check ───────────────────────────────
        if trade.is_target_hit(ltp):
            logger.info(
                "Target hit — trade_id=%s symbol=%s target=%.2f ltp=%.2f",
                trade.id, trade.instrument.symbol, trade.target_price, ltp,
            )
            trade.close(exit_price=ltp, exit_reason="target_hit")
            db.flush()
            self._emit(OrderSignal(
                signal_type=SignalType.EXIT,
                trade=trade,
                exit_reason="target_hit",
                candle=candle,
            ))
            self._rejection.clear_trade(trade.id)
            return

        # ── 3. Support rejection check ────────────────────────
        support_levels = SupportLevelRepo.get_active_for_instrument(db, trade.instrument_id)
        if support_levels:
            rejection = self._rejection.evaluate(
                trade, candle, prev_candle, support_levels
            )
            if rejection.should_exit:
                logger.info(
                    "Support rejection confirmed — trade_id=%s symbol=%s",
                    trade.id, trade.instrument.symbol,
                )
                self._alerter.support_rejection(
                    trade.instrument.symbol,
                    float(support_levels[0].price_level),
                )
                trade.close(exit_price=ltp, exit_reason="support_rejection")
                db.flush()
                self._emit(OrderSignal(
                    signal_type=SignalType.EXIT,
                    trade=trade,
                    exit_reason="support_rejection",
                    trail_result=rejection,
                    candle=candle,
                ))
                return

        # ── 4. Trailing SL ────────────────────────────────────
        trail = self._compute_trail(trade, candles)

        if trail.new_sl is not None:
            try:
                history = trade.update_sl(
                    new_sl=trail.new_sl,
                    method=trail.method,
                    reason=trail.trigger_reason,
                    price_at_time=ltp,
                    atr_value=trail.atr_value,
                    r_multiple=trail.r_multiple,
                )
                db.flush()

                logger.info(
                    "SL trailed — trade_id=%s %s  %.2f → %.2f  [%s / %s]",
                    trade.id, trade.instrument.symbol,
                    history.old_sl, trail.new_sl,
                    trail.method, trail.trigger_reason,
                )
                self._alerter.sl_trailed(
                    trade.instrument.symbol,
                    float(history.old_sl),
                    float(trail.new_sl),
                    trail.method,
                )
                self._emit(OrderSignal(
                    signal_type=SignalType.MODIFY_SL,
                    trade=trade,
                    new_sl=trail.new_sl,
                    trail_result=trail,
                    candle=candle,
                ))

            except ValueError as exc:
                # update_sl raises if new SL doesn't improve position
                logger.debug("SL trail skipped trade_id=%s: %s", trade.id, exc)

    # ── Trail method selector ─────────────────────────────────

    def _compute_trail(self, trade: Trade, candles: list) -> TrailResult:
        """
        Select trail method: per-trade override → system_configs default.
        Default is R-multiple as configured.
        """
        method = trade.sl_method or self._config.get("trail_method", "r_multiple")

        atr_period     = int(self._config.get("atr_period",     14))
        atr_multiplier = float(self._config.get("atr_multiplier", 1.5))
        ema_period     = int(self._config.get("ema_trail_period", 21))
        swing_lookback = int(self._config.get("swing_lookback",   5))

        if method == "atr":
            return ATRTrail.compute_new_sl(
                trade, candles,
                atr_period=atr_period,
                atr_multiplier=atr_multiplier,
            )
        if method == "swing":
            return SwingTrail.compute_new_sl(
                trade, candles,
                lookback=swing_lookback,
            )
        if method == "ema":
            return EMATrail.compute_new_sl(
                trade, candles,
                period=ema_period,
            )
        # default: r_multiple
        return RMultipleTrail.compute_new_sl(trade, candles)

    # ── Signal emission ───────────────────────────────────────

    def _emit(self, signal: OrderSignal) -> None:
        for handler in self._signal_handlers:
            try:
                handler(signal)
            except Exception as exc:
                logger.error("Signal handler error: %s", exc, exc_info=True)

    # ── Config ────────────────────────────────────────────────

    def _load_config(self) -> None:
        with get_db() as db:
            self._config = MarketConfigRepo.get_all(db, self.user_id)
            self._alerter = Alerter(
                bot_token=self._config.get("telegram_bot_token", ""),
                chat_id=self._config.get("telegram_chat_id", ""),
            )
        self._initialised = True
        logger.info(
            "StrategyEngine: loaded config — default trail method: %s",
            self._config.get("trail_method", "r_multiple"),
        )

    def reload_config(self) -> None:
        """Hot-reload config from DB without restarting."""
        self._load_config()
        logger.info("StrategyEngine: config reloaded")