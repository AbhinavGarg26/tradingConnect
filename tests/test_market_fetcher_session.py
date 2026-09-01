import ast
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


# Load this small pure helper without importing the fetcher's external services.
SOURCE = Path(__file__).parents[1].joinpath("kite_market_fetcher.py").read_text()
TREE = ast.parse(SOURCE)
FUNCTION = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "quote_is_current_session"
)
MODULE = ast.Module(body=[FUNCTION], type_ignores=[])
NAMESPACE = {"pd": pd, "IST": ZoneInfo("Asia/Kolkata"), "datetime": datetime}
exec(compile(MODULE, "kite_market_fetcher.py", "exec"), NAMESPACE)
quote_is_current_session = NAMESPACE["quote_is_current_session"]


def test_quote_is_current_session_rejects_previous_trading_day():
    now = datetime(2026, 9, 2, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))

    assert not quote_is_current_session({"timestamp": "2026-09-01 15:30:00"}, now)


def test_quote_is_current_session_accepts_current_day_and_missing_timestamp():
    now = datetime(2026, 9, 2, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))

    assert quote_is_current_session({"timestamp": "2026-09-02 10:14:55+05:30"}, now)
    assert quote_is_current_session({}, now)
