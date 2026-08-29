from database.market_price import get_buy_average_price
from market.market_gtt import update_or_place_gtt, get_active_gtt_orders, cleanup_duplicate_gtts, \
    extract_gtt_trigger_price
from utilities.conversion.number_conversion import round_to_tick
import math


def process_open_positions(ignore_symbol, pct_loss, logger, kite, db):
    positions_response = kite.positions()
    net_positions = positions_response.get("net", [])

    open_positions = [p for p in net_positions if p["quantity"] > 0]

    if not open_positions:
        logger.info("No open long positions found.")
        return 0

    active_gtts_map = get_active_gtt_orders(kite)

    for pos in open_positions:
        raw_symbol = pos["tradingsymbol"]
        symbol = str(raw_symbol).strip().upper()
        if symbol in ignore_symbol:
            logger.info(f"[{symbol}] Ignored")
            continue

        buy_price = get_buy_average_price(logger, db, symbol, pos["average_price"])
        ltp = pos["last_price"]
        quantity = pos["quantity"]

        if buy_price <= 0:
            continue

        pnl_pct = ((ltp - buy_price) / buy_price) * 100
        logger.info(f"[{symbol}] Qty: {quantity} | Buy Avg: ₹{buy_price:.2f} | LTP: ₹{ltp:.2f} | P&L: {pnl_pct:.2f}%")

        existing_gtt_list = active_gtts_map.get(symbol, [])
        existing_gtt = cleanup_duplicate_gtts(logger, kite, symbol, existing_gtt_list)
        current_trigger = extract_gtt_trigger_price(existing_gtt) if existing_gtt else None

        # RULE 1: Hard Stop (> 8% Loss) -> Set/Modify GTT trigger to (LTP - 1.0)
        if pnl_pct <= -pct_loss:
            raw_trigger = ltp - 1.0
            raw_limit = raw_trigger * 0.995
            target_trigger_ticked = round_to_tick(raw_trigger)

            # Skip modify API call if trigger is already positioned near LTP - 1.0
            if current_trigger is not None and abs(current_trigger - target_trigger_ticked) <= 0.10:
                logger.info(
                    f"[{symbol}] Existing GTT trigger already set at ₹{current_trigger:.2f} for {pct_loss}% Loss. Skipping.")
                continue

            logger.warning(
                f"[{symbol}] Loss is {pnl_pct:.2f}% (>={pct_loss}%). Updating GTT trigger to ₹{target_trigger_ticked:.2f}")
            update_or_place_gtt(
                logger,
                kite=kite,
                position=pos,
                raw_trigger_price=raw_trigger,
                raw_limit_price=raw_limit,
                order_tag="HARD_EXIT_LOSSPCT",
                existing_gtt=existing_gtt
            )
            continue

        # RULE 2: Trailing Stop Loss for Profit >= 10%
        if pnl_pct >= 10.0:
            # Trailing calculation: +5% SL at 10% gain, +10% SL at 15% gain, +15% SL at 20% gain...
            steps_above_base = math.floor((pnl_pct - 10.0) / 5.0)
            target_sl_pct = 5.0 + (steps_above_base * 5.0)

            raw_target_sl_price = buy_price * (1 + (target_sl_pct / 100.0))
            raw_limit_price = raw_target_sl_price * 0.995
            target_sl_price_ticked = round_to_tick(raw_target_sl_price)

            should_update = False
            if existing_gtt is None:
                should_update = True
            elif current_trigger is not None:
                # Update ONLY if target SL price moves higher than current trigger
                if target_sl_price_ticked > current_trigger + 0.01:
                    should_update = True
                else:
                    logger.info(
                        f"[{symbol}] Existing GTT trigger (₹{current_trigger:.2f}) is already up to target (₹{target_sl_price_ticked:.2f}). Skipping.")

            if should_update:
                logger.info(
                    f"[{symbol}] Profit is {pnl_pct:.2f}%. Updating GTT trigger to +{target_sl_pct:.1f}% (₹{target_sl_price_ticked:.2f})")
                update_or_place_gtt(
                    logger,
                    kite=kite,
                    position=pos,
                    raw_trigger_price=raw_target_sl_price,
                    raw_limit_price=raw_limit_price,
                    order_tag="PROFIT_TRAILING_SL",
                    existing_gtt=existing_gtt
                )

    return open_positions.count
