from __future__ import annotations

import sys

from dotenv import load_dotenv
load_dotenv()

from trading.database import get_db
from trading.exchange_link import ExchangeLinkRepo
from trading.repositories import UserRepo

from kiteconnect import KiteConnect


def fetch_user_token(logger):
    with get_db() as db:
        user = UserRepo.get_active(db)
        if not user:
            logger.critical("No active user in DB — exiting")
            sys.exit(1)

        link = ExchangeLinkRepo.get_for_user(db, user.id)
        if not link or not link.is_session_valid:
            logger.critical(
                "Session token expired or missing. "
                "Paste a fresh token via Rails UI first."
            )
            sys.exit(1)

        api_key      = link.decrypt_access_id(db)
        api_secret = link.decrypt_access_secret(db)
        access_token = link.decrypt_session_token(db)
        user_id      = user.id

    # Build Kite client
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    return kite, user_id