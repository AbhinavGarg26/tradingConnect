"""Compatibility helpers for Kite order parameters newer than SDK 5.0.1."""

from __future__ import annotations

import inspect


AUTO_MARKET_PROTECTION = -1


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
