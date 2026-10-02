# groww_order_probe.py
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

load_dotenv("/home/deployer/apps/tradingConnect/.env")

from trading.database import get_db
from trading.exchange_link import ExchangeLinkRepo

USER_ID = int(os.environ["GROWW_USER_ID"])

# Change these values for the intended test order.
ORDER = {
    "trading_symbol": os.environ["GROWW_SYMBOL"],       # e.g. NIFTY26O1723000PE
    "quantity": int(os.environ["GROWW_QUANTITY"]),      # must respect lot size
    "price": float(os.environ["GROWW_PRICE"]),          # use a safe LIMIT price
    "validity": "DAY",
    "exchange": os.environ.get("GROWW_EXCHANGE", "NSE"),
    "segment": os.environ.get("GROWW_SEGMENT", "FNO"),
    "product": os.environ.get("GROWW_PRODUCT", "NRML"),
    "order_type": "LIMIT",
    "transaction_type": os.environ.get("GROWW_SIDE", "BUY"),
    "order_reference_id": os.environ.get("GROWW_REFERENCE", "groww-probe-01"),
}

if os.environ.get("CONFIRM_LIVE_ORDER") != "YES":
    sys.exit("Refusing to place a live order. Set CONFIRM_LIVE_ORDER=YES.")

with get_db() as db:
    link = ExchangeLinkRepo.get_for_user(db, USER_ID, provider="groww")
    if not link or not link.is_session_valid:
        sys.exit("Groww connection/token is missing or expired.")
    token = link.decrypt_session_token(db)

request = Request(
    "https://api.groww.in/v1/order/create",
    data=json.dumps(ORDER).encode("utf-8"),
    method="POST",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-VERSION": "1.0",
    },
)

try:
    with urlopen(request, timeout=15) as response:
        print(f"HTTP {response.status}")
        print(response.read().decode("utf-8"))
except HTTPError as error:
    print(f"HTTP {error.code}")
    print(error.read().decode("utf-8"))
except URLError as error:
    print(f"Network error: {error.reason}")
except Exception as error:
    print(f"{type(error).__name__}: {error}")