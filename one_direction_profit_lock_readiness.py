"""Fail-closed evidence gate for Profit Lock protected-live promotion."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from nifty_15m_env import load_nifty_environment
load_nifty_environment()

from trading.one_direction_profit_lock_journal import KEY
from trading.strategy_records import StrategyEvent, StrategyRun, StrategyTrade

MIN_TRADES = 30
MIN_SESSIONS = 10
MIN_PROFIT_LOCKS = 5
MIN_REVERSALS = 2


def forward_evidence(factory, user_id):
    with factory() as db:
        trades = list(db.scalars(select(StrategyTrade).where(
            StrategyTrade.user_id == user_id, StrategyTrade.strategy_key == KEY,
            StrategyTrade.mode == "paper", StrategyTrade.status == "closed",
            StrategyTrade.entry_premium.is_not(None), StrategyTrade.exit_premium.is_not(None),
            func.coalesce(StrategyTrade.error_message, "").not_like("Day-end paper index proxy;%"),
        )))
        run_ids = {trade.strategy_run_id for trade in trades}
        events = list(db.scalars(select(StrategyEvent).where(StrategyEvent.strategy_run_id.in_(run_ids)))) if run_ids else []
    sessions = {trade.trading_date for trade in trades}
    locks = sum(event.action == "PROFIT_LOCK_UPDATED" for event in events)
    reversals = sum(event.reason == "reversal" and event.action.startswith("EXIT_") for event in events)
    complete = (len(trades) >= MIN_TRADES and len(sessions) >= MIN_SESSIONS
                and locks >= MIN_PROFIT_LOCKS and reversals >= MIN_REVERSALS)
    return {"complete": complete, "closed_option_trades": len(trades),
            "sessions": len(sessions), "profit_lock_events": locks, "reversal_exits": reversals,
            "requirements": {"trades": MIN_TRADES, "sessions": MIN_SESSIONS,
                             "profit_locks": MIN_PROFIT_LOCKS, "reversals": MIN_REVERSALS}}


def readiness(factory, user_id, option_report):
    path = Path(option_report)
    option = json.loads(path.read_text()) if path.exists() else {
        "status": "blocked_missing_historical_option_bid_ask", "accepted": False}
    forward = forward_evidence(factory, user_id)
    return {"ready": bool(option.get("accepted")) and forward["complete"],
            "option_backtest": option, "forward_paper": forward,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def assert_live_ready(factory, user_id, option_report):
    report = readiness(factory, user_id, option_report)
    if not report["ready"]:
        raise RuntimeError("Profit Lock live evidence gate failed; review option backtest and forward-paper report")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--option-report", default="outputs/one-direction-option-backtest.json")
    parser.add_argument("--output", default="outputs/one-direction-live-readiness.json")
    args = parser.parse_args()
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    report = readiness(get_db, args.user_id, args.option_report)
    Path(args.output).write_text(json.dumps(report, indent=2)); print(json.dumps(report, indent=2))


if __name__ == "__main__": main()
