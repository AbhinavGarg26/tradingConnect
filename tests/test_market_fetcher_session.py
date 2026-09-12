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

OWNERSHIP_ASSIGNMENT = next(
    node for node in TREE.body
    if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == "MARKET_BREATH_OWNED_SYMBOLS" for target in node.targets)
)
OWNERSHIP_FUNCTION = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "should_persist_market_snapshot"
)
OWNERSHIP_NAMESPACE = {}
exec(
    compile(ast.Module(body=[OWNERSHIP_ASSIGNMENT, OWNERSHIP_FUNCTION], type_ignores=[]), "kite_market_fetcher.py", "exec"),
    OWNERSHIP_NAMESPACE,
)
should_persist_market_snapshot = OWNERSHIP_NAMESPACE["should_persist_market_snapshot"]

PLAN_ASSIGNMENT = next(
    node for node in TREE.body
    if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == "ANALYSIS_TIMEFRAMES" for target in node.targets)
)
PLAN_FUNCTION = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "due_analysis_timeframes"
)
REFERENCE_FUNCTION = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "analysis_freshness_reference"
)
PLAN_NAMESPACE = {"timedelta": __import__("datetime").timedelta, "pd": pd, "IST": ZoneInfo("Asia/Kolkata"), "datetime": datetime}
exec(compile(ast.Module(body=[PLAN_ASSIGNMENT, REFERENCE_FUNCTION, PLAN_FUNCTION], type_ignores=[]), "kite_market_fetcher.py", "exec"), PLAN_NAMESPACE)
due_analysis_timeframes = PLAN_NAMESPACE["due_analysis_timeframes"]
analysis_freshness_reference = PLAN_NAMESPACE["analysis_freshness_reference"]


def test_quote_is_current_session_rejects_previous_trading_day():
    now = datetime(2026, 9, 2, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))

    assert not quote_is_current_session({"timestamp": "2026-09-01 15:30:00"}, now)


def test_quote_is_current_session_accepts_current_day_and_missing_timestamp():
    now = datetime(2026, 9, 2, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))

    assert quote_is_current_session({"timestamp": "2026-09-02 10:14:55+05:30"}, now)
    assert quote_is_current_session({}, now)


def test_market_breath_is_the_only_nifty_snapshot_writer():
    assert not should_persist_market_snapshot("NIFTY 50")
    assert should_persist_market_snapshot("NIFTY BANK")


def test_missing_and_stale_analysis_timeframes_are_due():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    latest = {
        "15m": now - __import__("datetime").timedelta(minutes=10),
        "1h": now - __import__("datetime").timedelta(hours=3),
        "3h": now - __import__("datetime").timedelta(hours=1),
    }

    due = due_analysis_timeframes(latest, now)

    assert "15m" not in due
    assert "3h" not in due
    assert "1h" in due
    assert "1d" in due
    assert "1w" in due
    assert "1mo" in due


def test_weekend_does_not_make_friday_candles_stale():
    saturday = datetime(2026, 9, 12, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    friday_candle = datetime(2026, 9, 11, 15, 15, tzinfo=ZoneInfo("Asia/Kolkata"))

    due = due_analysis_timeframes({"15m": friday_candle}, saturday)

    assert "15m" not in due
    assert analysis_freshness_reference(saturday).strftime("%a %H:%M") == "Fri 15:30"


def test_new_instrument_discovery_uses_targeted_refresh_not_full_market_job():
    discovery = next(
        node for node in TREE.body
        if isinstance(node, ast.FunctionDef) and node.name == "discover_new_watchlist_instruments"
    )
    calls = [
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(discovery)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    ]

    assert "refresh_new_watchlist_instrument" in calls
    assert "fetch_and_write" not in calls


def test_new_instrument_quote_is_saved_before_history_hydration():
    refresh = next(
        node for node in TREE.body
        if isinstance(node, ast.FunctionDef) and node.name == "refresh_new_watchlist_instrument"
    )
    calls = [
        node.func.id
        for node in ast.walk(refresh)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert calls.index("update_watchlist_quote") < calls.index("hydrate_missing_analysis_snapshots")
