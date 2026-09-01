from database.live_market_state import (
    prune_inactive_position_risk,
    sync_instrument_live_state,
    sync_position_risk_state,
)
from market.market_exit import MarketExitExecutor
from market.entry_price_tracker import CurrentEntryPriceTracker
from market.position_ltp_stream import PositionLtpStream
from market.position_stops import PositionStopTracker


ESTIMATED_ROUND_TRIP_CHARGES = 55.0
CHARGE_FLOOR_SAFETY_BUFFER_PCT = 0.5


def _position_key(position: dict) -> str:
    return ":".join([
        str(position.get("exchange", "")),
        str(position.get("tradingsymbol", "")),
        str(position.get("product", "")),
    ])


def _rest_ltp_fallback(kite, positions: list[dict], missing_tokens: set[int]) -> dict[int, float]:
    """Fetch missing/stale prices in one quote call; never use positions.last_price."""
    instruments = [
        f"{position['exchange']}:{position['tradingsymbol']}"
        for position in positions
        if int(position.get("instrument_token", 0)) in missing_tokens
    ]
    if not instruments:
        return {}

    quotes = kite.ltp(instruments)
    result = {}
    for position in positions:
        token = int(position.get("instrument_token", 0))
        if token not in missing_tokens:
            continue
        key = f"{position['exchange']}:{position['tradingsymbol']}"
        quote = quotes.get(key, {})
        if quote.get("last_price") is not None:
            result[token] = float(quote["last_price"])
    return result


def process_open_positions(
    ignore_symbol,
    pct_loss,
    logger,
    kite,
    db,
    price_stream: PositionLtpStream,
    stop_tracker: PositionStopTracker,
    exit_executor: MarketExitExecutor,
    entry_price_tracker: CurrentEntryPriceTracker,
    publish_live_state: bool = False,
):
    positions_response = kite.positions()
    net_positions = positions_response.get("net", [])
    open_positions = [position for position in net_positions if position["quantity"] > 0]
    active_keys = {_position_key(position) for position in open_positions}

    if publish_live_state:
        try:
            with db.begin_nested():
                prune_inactive_position_risk(db, active_keys)
        except Exception as exc:
            logger.error("Live position-state cleanup failed: %s", exc)

    if not open_positions:
        price_stream.update_tokens([])
        stop_tracker.remove_missing(set())
        exit_executor.remove_missing(set())
        logger.info("No open long positions found.")
        return 0

    tokens = {int(position["instrument_token"]) for position in open_positions}
    price_stream.update_tokens(tokens)
    live_prices = {token: price_stream.get_price(token) for token in tokens}
    missing_tokens = {token for token, price in live_prices.items() if price is None}
    if missing_tokens:
        try:
            fallback_prices = _rest_ltp_fallback(kite, open_positions, missing_tokens)
            live_prices.update(fallback_prices)
            price_stream.record_rest_prices(fallback_prices)
        except Exception as exc:
            logger.error("Fresh REST LTP fallback failed: %s", exc)

    stop_tracker.remove_missing(active_keys)
    exit_executor.remove_missing(active_keys)
    entry_price_tracker.remove_missing(active_keys)

    for position in open_positions:
        symbol = str(position["tradingsymbol"]).strip().upper()
        if symbol in ignore_symbol:
            logger.info("[%s] Ignored", symbol)
            continue

        if not exit_executor.remove_legacy_gtts(position):
            logger.critical("[%s] Risk evaluation paused until legacy GTT cleanup succeeds", symbol)
            continue

        token = int(position["instrument_token"])
        ltp = live_prices.get(token)
        if ltp is None or ltp <= 0:
            logger.critical("[%s] No fresh WebSocket or REST LTP; stop cannot be evaluated", symbol)
            continue

        position_key = _position_key(position)
        try:
            buy_price, entry_changed = entry_price_tracker.resolve(kite, position)
        except Exception as exc:
            logger.critical("[%s] Cannot resolve execution-derived entry price: %s", symbol, exc)
            continue
        if entry_changed:
            stop_tracker.reset(position_key)
            exit_executor.reset_position(position_key)
            logger.warning("[%s] New broker execution lifecycle detected; stop state reset", symbol)

        pnl_pct = ((ltp - buy_price) / buy_price) * 100
        position_value = buy_price * int(position["quantity"])
        charge_floor_pct = (
            (ESTIMATED_ROUND_TRIP_CHARGES / position_value) * 100
            + CHARGE_FLOOR_SAFETY_BUFFER_PCT
        )
        logger.info(
            "[%s] Qty: %s | Buy Avg: ₹%.2f | Live LTP: ₹%.2f | P&L: %.2f%%",
            symbol, position["quantity"], buy_price, ltp, pnl_pct,
        )

        exit_reason = stop_tracker.evaluate(
            position_key=position_key,
            pnl_pct=pnl_pct,
            soft_loss_pct=pct_loss,
            recent_prices=price_stream.recent_prices(token),
            charge_floor_pct=charge_floor_pct,
        )
        if exit_reason:
            exit_executor.exit_position(position, exit_reason)

        if publish_live_state:
            try:
                with db.begin_nested():
                    sync_instrument_live_state(
                        db,
                        price_stream,
                        entity_key=f"{position['exchange']}:{symbol}",
                        instrument_token=token,
                    )
                    stop_state = stop_tracker.snapshot(_position_key(position))
                    if stop_state:
                        sync_position_risk_state(
                            db,
                            position=position,
                            ltp=ltp,
                            buy_price=buy_price,
                            pnl_pct=pnl_pct,
                            soft_loss_pct=pct_loss,
                            stop_state=stop_state,
                            exit_reason=exit_reason,
                        )
            except Exception as exc:
                logger.error("[%s] Live-state publish failed: %s", symbol, exc)

    return len(open_positions)
