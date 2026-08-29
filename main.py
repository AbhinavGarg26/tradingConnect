from kiteconnect import KiteConnect
from utilities.manipulating_file.manipulating_file import fetch_instrument_list
import logging
import sys

from dotenv import load_dotenv
load_dotenv()

from trading.user_token import fetch_user_token

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("instrument_sync")

kite, user_id = fetch_user_token(logger)

# Fetch all orders
response = kite.orders()

# Get instruments
fetch_instrument_list(kite)