import logging
logging.basicConfig(level=logging.DEBUG)


def place_orders(kite):
    try:
        order_id = kite.place_order(tradingsymbol="INFY",
                                    exchange=kite.EXCHANGE_NSE,
                                    transaction_type=kite.TRANSACTION_TYPE_BUY,
                                    quantity=1,
                                    variety=kite.VARIETY_AMO,
                                    order_type=kite.ORDER_TYPE_MARKET,
                                    product=kite.PRODUCT_CNC,
                                    validity=kite.VALIDITY_DAY)

        logging.info("Order placed. ID is: {}".format(order_id))
    except Exception as e:
        logging.info("Order placement failed: {}".format(e.message))


def place_mf_orders(kite):
    # Place an mutual fund order
    kite.place_mf_order(
        tradingsymbol="INF090I01239",
        transaction_type=kite.TRANSACTION_TYPE_BUY,
        amount=5000,
        tag="mytag"
    )

    # Cancel a mutual fund order
    kite.cancel_mf_order(order_id="order_id")

    # Get mutual fund instruments
    kite.mf_instruments()