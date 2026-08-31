from database.market_price import get_buy_average_price
from market.market_exit import MarketExitExecutor
from market.position_ltp_stream import PositionLtpStream
from market.position_stops import PositionStopTracker


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
):
    positions_response = kite.positions()
    net_positions = positions_response.get("net", [])
    open_positions = [position for position in net_positions if position["quantity"] > 0]

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

    active_keys = {_position_key(position) for position in open_positions}
    stop_tracker.remove_missing(active_keys)
    exit_executor.remove_missing(active_keys)

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

        buy_price = get_buy_average_price(logger, db, symbol, position["average_price"])
        if buy_price <= 0:
            continue

        pnl_pct = ((ltp - buy_price) / buy_price) * 100
        logger.info(
            "[%s] Qty: %s | Buy Avg: ₹%.2f | Live LTP: ₹%.2f | P&L: %.2f%%",
            symbol, position["quantity"], buy_price, ltp, pnl_pct,
        )

        exit_reason = stop_tracker.evaluate(
            position_key=_position_key(position),
            pnl_pct=pnl_pct,
            soft_loss_pct=pct_loss,
            recent_prices=price_stream.recent_prices(token),
        )
        if exit_reason:
            exit_executor.exit_position(position, exit_reason)

    return len(open_positions)
