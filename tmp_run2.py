# groww_open_orders.py
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
load_dotenv("/home/deployer/apps/tradingConnect/.env")

from trading.database import get_db
from trading.exchange_link import ExchangeLinkRepo

USER_ID = 975447485
BASE_URL = "https://api.groww.in/v1"

def groww_get(token, path, params):
    request = Request(f"{BASE_URL}{path}?{urlencode(params)}")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    request.add_header("X-API-VERSION", "1.0")

    with urlopen(request, timeout=15) as response:
        body = json.loads(response.read())

    if body.get("status") != "SUCCESS":
        raise RuntimeError(body.get("message") or body)
    return body["payload"]

with get_db() as db:
    link = ExchangeLinkRepo.get_for_user(db, USER_ID, provider="groww")
    if not link or not link.is_session_valid:
        raise RuntimeError("Groww token is missing or expired.")
    token = link.decrypt_session_token(db)

for segment in ("CASH", "FNO"):
    payload = groww_get(token, "/order/list", {
        "segment": segment,
        "page": 0,
        "page_size": 100,
    })

    orders = payload.get("order_list", [])
    open_orders = [
        order for order in orders
        if str(order.get("order_status", "")).upper()
        in {"OPEN", "PENDING", "ACKED", "TRIGGER_PENDING"}
    ]

    print(f"\n{segment}: {len(open_orders)} open/pending orders")
    for order in open_orders:
        print(json.dumps({
            "groww_order_id": order.get("groww_order_id"),
            "symbol": order.get("trading_symbol"),
            "status": order.get("order_status"),
            "side": order.get("transaction_type"),
            "quantity": order.get("quantity"),
            "filled_quantity": order.get("filled_quantity"),
            "price": order.get("price"),
            "trigger_price": order.get("trigger_price"),
            "exchange": order.get("exchange"),
            "product": order.get("product"),
            "segment": order.get("segment"),
            "amo_status": order.get("amo_status"),
        }, indent=2))