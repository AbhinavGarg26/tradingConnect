"""
replay.py — replays historical candle data through the strategy engine.
"""

from dotenv import load_dotenv
load_dotenv()

import trading.utils as _utils
_utils.FORCE_MARKET_OPEN = True

import time
import logging
from datetime import datetime, date
from decimal import Decimal

from kiteconnect import KiteConnect

from trading.candle_builder import Candle
from trading.database import get_db
from trading.exchange_link import ExchangeLinkRepo
from trading.repositories import InstrumentRepo, MarketConfigRepo, TradeRepo, UserRepo
from trading.alerts import Alerter
from trading.strategy_engine import StrategyEngine, SignalType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("replay")

REPLAY_DATE      = date(2026, 3, 20)
INSTRUMENT_TOKEN = 256265
TRADING_SYMBOL   = "NIFTY 50"
REPLAY_SPEED     = 0.0


def fetch_candles(kite, token, replay_date):
    from_dt = datetime.combine(replay_date, datetime.min.time())
    to_dt   = datetime.combine(replay_date, datetime.max.time())
    logger.info("Fetching candles for token=%s on %s", token, replay_date)
    candles = kite.historical_data(
        instrument_token=token,
        from_date=from_dt,
        to_date=to_dt,
        interval="minute",
    )
    logger.info("Fetched %d candles", len(candles))
    return candles


def build_candle(raw, token):
    return Candle(
        instrument_token=token,
        timeframe_minutes=1,
        open=Decimal(str(raw["open"])),
        high=Decimal(str(raw["high"])),
        low=Decimal(str(raw["low"])),
        close=Decimal(str(raw["close"])),
        volume=raw.get("volume", 0),
        open_time=raw["date"],
        close_time=raw["date"],
        is_closed=True,
        tick_count=1,
    )


def main():
    print("=" * 60)
    print(f"  REPLAY — {REPLAY_DATE} — {TRADING_SYMBOL}")
    print("=" * 60)

    with get_db() as db:
        user          = UserRepo.get_active(db)
        user_id       = user.id
        cfg           = MarketConfigRepo.get_all(db, user_id)
        inst          = InstrumentRepo.get_by_token(db, INSTRUMENT_TOKEN)

        if not inst:
            print(f"ERROR: Token {INSTRUMENT_TOKEN} not in DB")
            return

        instrument_id = inst.id
        open_trades   = TradeRepo.get_open_by_instrument(db, instrument_id)

        if not open_trades:
            print("\nERROR: No open trades. Run quick_test.py first.")
            return

        print(f"\nFound {len(open_trades)} open trade(s):")
        for t in open_trades:
            print(f"  {t.direction:<5} entry=₹{t.entry_price}  sl=₹{t.current_sl}  "
                  f"target=₹{t.target_price or '—'}  method={t.sl_method}")

        link = ExchangeLinkRepo.get_for_user(db, user_id)
        if not link or not link.is_session_valid:
            print("\nERROR: No valid session token.")
            return
        api_key      = link.decrypt_access_id(db)
        access_token = link.decrypt_session_token(db)

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    try:
        raw_candles = fetch_candles(kite, INSTRUMENT_TOKEN, REPLAY_DATE)
    except Exception as e:
        print(f"\nERROR fetching data: {e}")
        return

    if not raw_candles:
        print("No candles returned.")
        return

    strategy = StrategyEngine(user_id=user_id)
    strategy._alerter = Alerter(bot_token="", chat_id="")
    signals_received = []

    def capture_signal(signal):
        signals_received.append(signal)
        if signal.signal_type == SignalType.EXIT:
            print(f"\n  🔴 EXIT | reason={signal.exit_reason} | "
                  f"close=₹{signal.candle.close if signal.candle else '—'}")
        elif signal.signal_type == SignalType.MODIFY_SL:
            print(f"\n  📈 TRAIL | new_sl=₹{signal.new_sl} | "
                  f"method={signal.trail_result.method} | "
                  f"reason={signal.trail_result.trigger_reason} | "
                  f"R={signal.trail_result.r_multiple or '—'}")

    strategy.on_signal(capture_signal)

    print(f"\nReplaying {len(raw_candles)} candles...")
    print(f"  First: {raw_candles[0]['date']}  Last: {raw_candles[-1]['date']}")
    day_low  = min(c['low']  for c in raw_candles)
    day_high = max(c['high'] for c in raw_candles)
    print(f"  Range: ₹{day_low} — ₹{day_high}")
    print("-" * 60)

    last_candle_index = 0
    candles_processed = 0
    for i, raw in enumerate(raw_candles):
        candle = build_candle(raw, INSTRUMENT_TOKEN)

        if i % 15 == 0:
            print(f"[{i + 1:>4}/{len(raw_candles)}] "
                  f"{candle.open_time} | "
                  f"O={candle.open} H={candle.high} L={candle.low} C={candle.close}")

        # Use evaluate() only — do NOT also push to _store manually
        strategy.evaluate(candle, user_id)

        if REPLAY_SPEED > 0:
            time.sleep(REPLAY_SPEED)

        candles_processed += 1

    print("\n" + "=" * 60)
    print("REPLAY COMPLETE")
    print(f"  Candles processed : {last_candle_index + 1}")
    print(f"  Signals generated : {len(signals_received)}")

    if signals_received:
        trails = [s for s in signals_received if s.signal_type == SignalType.MODIFY_SL]
        exits  = [s for s in signals_received if s.signal_type == SignalType.EXIT]
        print(f"  SL trails         : {len(trails)}")
        print(f"  Exit signals      : {len(exits)}")
        if trails:
            print("\nSL Trail history:")
            for s in trails:
                print(f"  ₹{s.new_sl:<10} [{s.trail_result.method}]  "
                      f"{s.trail_result.trigger_reason}  "
                      f"R={s.trail_result.r_multiple or '—'}")
        if exits:
            print("\nExit detail:")
            for s in exits:
                print(f"  reason={s.exit_reason}  "
                      f"close=₹{s.candle.close if s.candle else '—'}")
    else:
        print("\n  No signals — check entry/SL vs day range:")
        print(f"  Day range : ₹{day_low} — ₹{day_high}")
        with get_db() as db:
            remaining = TradeRepo.get_open_by_instrument(db, instrument_id)
            if remaining:
                t = remaining[0]
                print(f"  Entry     : ₹{t.entry_price}")
                print(f"  SL        : ₹{t.current_sl}")
                print(f"  Target    : ₹{t.target_price or '—'}")

    print("=" * 60)


if __name__ == "__main__":
    main()