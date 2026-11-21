"""Compatibility helpers for Kite order parameters newer than SDK 5.0.1."""

from __future__ import annotations

import inspect
from decimal import Decimal

from kiteconnect import KiteConnect
from kiteconnect.exceptions import InputException


AUTO_MARKET_PROTECTION = -1


def place_market_order_with_depth_fallback(kite, **params):
    """Submit a market order, or use the best executable depth price if Kite blocks it.

    Kite blocks unprotected MARKET orders for some illiquid stock options.  Only
    that specific rejection is retried: all other broker errors propagate so an
    invalid or uncertain order is never silently resubmitted.
    """
    try:
        return place_protected_market_order(kite, **params)
    except InputException as exc:
        if "MARKET orders are blocked for stock options due to illiquidity" not in str(exc):
            raise

    exchange = params["exchange"]
    tradingsymbol = params["tradingsymbol"]
    quote_key = f"{exchange}:{tradingsymbol}"
    quote = (kite.quote([quote_key]).get(quote_key) or {})
    side = "buy" if params["transaction_type"] == KiteConnect.TRANSACTION_TYPE_SELL else "sell"
    levels = ((quote.get("depth") or {}).get(side) or [])
    if not levels or not levels[0].get("price"):
        raise InputException(f"No executable {side} depth available for {quote_key}; limit fallback not submitted")

    limit_params = dict(params)
    limit_params["order_type"] = KiteConnect.ORDER_TYPE_LIMIT
    limit_params["price"] = float(Decimal(str(levels[0]["price"])))
    return kite.place_order(**limit_params)


def place_protected_market_order(kite, *, market_protection: int = AUTO_MARKET_PROTECTION, **params):
    """Place MARKET/SL-M with protection across old and new Kite SDK versions."""
    signature = inspect.signature(kite.place_order)
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if "market_protection" in signature.parameters or accepts_kwargs:
        return kite.place_order(market_protection=market_protection, **params)

    # KiteConnect 5.0.1 has no public market_protection argument, although its
    # authenticated order endpoint accepts the parameter.
    request_params = {**params, "market_protection": market_protection}
    variety = request_params.pop("variety")
    request_params = {
        key: value for key, value in request_params.items() if value is not None
    }
    response = kite._post(
        "order.place",
        url_args={"variety": variety},
        params=request_params,
    )
    return response["order_id"]
