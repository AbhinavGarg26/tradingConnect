"""Execution-derived entry price for the current Groww position lifecycle."""

from __future__ import annotations


def execution_entry_price(orders: list[dict], position: dict) -> float | None:
    symbol = str(position.get("trading_symbol") or position.get("symbol") or "").upper()
    exchange = str(position.get("exchange") or "NSE").upper()
    product = str(position.get("product") or position.get("product_type") or "").upper()
    expected_quantity = int(position.get("quantity") or position.get("net_quantity") or 0)
    matching = [
        order for order in orders
        if str(order.get("trading_symbol") or "").upper() == symbol
        and str(order.get("exchange") or "").upper() == exchange
        and (not product or str(order.get("product") or "").upper() == product)
        and int(order.get("filled_quantity") or 0) > 0
        and float(order.get("average_fill_price") or 0) > 0
    ]
    matching.sort(key=lambda order: str(
        order.get("exchange_time") or order.get("created_at") or order.get("trade_date") or ""
    ))

    quantity = 0
    average = 0.0
    for order in matching:
        filled = int(order.get("filled_quantity") or 0)
        price = float(order.get("average_fill_price") or 0)
        if str(order.get("transaction_type") or "").upper() == "BUY":
            average = ((average * quantity) + (price * filled)) / (quantity + filled)
            quantity += filled
        elif str(order.get("transaction_type") or "").upper() == "SELL":
            quantity -= filled
            if quantity <= 0:
                quantity = 0
                average = 0.0

    return average if quantity == expected_quantity and quantity > 0 else None
