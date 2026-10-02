# groww_fno_gtt_probe.py
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

segment = os.environ.get("GROWW_SEGMENT", "FNO").upper()
if segment != "FNO":
    sys.exit("This probe is restricted to F&O. Set GROWW_SEGMENT=FNO.")

# A recovery GTT for a long position: when price rises to trigger_price,
# Groww submits a full-quantity SELL LIMIT order at order_price.
GTT = {
    "reference_id": os.environ.get("GROWW_REFERENCE", "groww-gtt-01"),
    "smart_order_type": "GTT",
    "segment": segment,
    "trading_symbol": os.environ["GROWW_SYMBOL"],
    "quantity": int(os.environ["GROWW_QUANTITY"]),
    "trigger_price": f"{float(os.environ['GROWW_TRIGGER_PRICE']):.2f}",
    "trigger_direction": os.environ.get("GROWW_TRIGGER_DIRECTION", "UP").upper(),
    "order": {
        "order_type": "LIMIT",
        "price": f"{float(os.environ['GROWW_ORDER_PRICE']):.2f}",
        "transaction_type": "SELL",
    },
    "product_type": os.environ.get("GROWW_PRODUCT", "NRML"),
    "exchange": os.environ.get("GROWW_EXCHANGE", "NSE"),
    "duration": "DAY",
}

if os.environ.get("CONFIRM_LIVE_ORDER") != "YES":
    sys.exit("Refusing to create a live GTT. Set CONFIRM_LIVE_ORDER=YES.")

with get_db() as db:
    link = ExchangeLinkRepo.get_for_user(db, USER_ID, provider="groww")
    if not link or not link.is_session_valid:
        sys.exit("Groww connection/token is missing or expired.")
    token = link.decrypt_session_token(db)

request = Request(
    "https://api.groww.in/v1/order-advance/create",
    data=json.dumps(GTT).encode("utf-8"),
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
