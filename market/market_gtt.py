from utilities.conversion.number_conversion import round_to_tick


def extract_gtt_trigger_price(gtt):
    """Safely extracts the trigger price from a GTT object."""
    condition = gtt.get("condition", {})
    trigger_values = condition.get("trigger_values", [])
    if trigger_values:
        return float(trigger_values[0])
    triggers = condition.get("triggers", [])
    if triggers:
        return float(triggers[0])
    return None


def get_active_gtt_orders(kite):
    """Fetch all active GTT orders mapped by normalized tradingsymbol."""
    gtts = kite.get_gtts()
    active_gtts = {}

    for gtt in gtts:
        if gtt.get("status") == "active":
            condition = gtt.get("condition", {})
            symbol = condition.get("tradingsymbol", "")

            if symbol:
                norm_symbol = str(symbol).strip().upper()
                if norm_symbol not in active_gtts:
                    active_gtts[norm_symbol] = []
                active_gtts[norm_symbol].append(gtt)

    return active_gtts


def cleanup_duplicate_gtts(logger, kite, symbol, active_gtt_list):
    """Retains only the latest active GTT and purges duplicates if any exist."""
    if not active_gtt_list:
        return None

    if len(active_gtt_list) > 1:
        logger.warning(f"[{symbol}] Found {len(active_gtt_list)} active GTTs! Purging older duplicates...")
        sorted_gtts = sorted(active_gtt_list, key=lambda x: x.get("id", 0), reverse=True)
        gtt_to_keep = sorted_gtts[0]

        for duplicate in sorted_gtts[1:]:
            gtt_id = duplicate["id"]
            try:
                kite.delete_gtt(gtt_id)
                logger.info(f"[{symbol}] Deleted duplicate GTT ID: {gtt_id}")
            except Exception as e:
                logger.error(f"[{symbol}] Failed to delete duplicate GTT {gtt_id}: {e}")

        return gtt_to_keep

    return active_gtt_list[0]


def update_or_place_gtt(logger, kite, position, raw_trigger_price, raw_limit_price, order_tag="TRAILING_SL", existing_gtt=None):
    """Modifies an existing active GTT if present; otherwise creates a new one."""
    symbol = position["tradingsymbol"]
    exchange = position["exchange"]
    quantity = position["quantity"]

    trigger_price = round_to_tick(raw_trigger_price)
    limit_price = round_to_tick(raw_limit_price)

    order_payload = [{
        "transaction_type": kite.TRANSACTION_TYPE_SELL,
        "quantity": quantity,
        "product": position["product"],
        "order_type": kite.ORDER_TYPE_LIMIT,
        "price": limit_price
    }]

    # REUSE: Modify existing GTT if present
    if existing_gtt:
        gtt_id = existing_gtt["id"]
        logger.info(
            f"[{symbol}] Modifying EXISTING GTT (ID: {gtt_id}) -> New Trigger: ₹{trigger_price:.2f} | Limit: ₹{limit_price:.2f}")
        try:
            kite.modify_gtt(
                trigger_id=gtt_id,
                trigger_type=kite.GTT_TYPE_SINGLE,
                tradingsymbol=symbol,
                exchange=exchange,
                trigger_values=[trigger_price],
                last_price=position["last_price"],
                orders=order_payload
            )
            logger.info(f"[{symbol}] Successfully modified GTT ID: {gtt_id}")
            return
        except Exception as e:
            logger.error(f"[{symbol}] Failed to modify GTT {gtt_id}, falling back to placement: {e}")

    # CREATE: Place new GTT if none exists
    logger.info(f"[{symbol}] Creating NEW {order_tag} GTT -> Trigger: ₹{trigger_price:.2f} | Limit: ₹{limit_price:.2f}")
    gtt_response = kite.place_gtt(
        trigger_type=kite.GTT_TYPE_SINGLE,
        tradingsymbol=symbol,
        exchange=exchange,
        trigger_values=[trigger_price],
        last_price=position["last_price"],
        orders=order_payload
    )
    logger.info(f"[{symbol}] Placed new GTT (Trigger ID: {gtt_response.get('trigger_id')})")