"""Fail-closed evidence gate for Profit Lock protected-live promotion."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, text

from nifty_15m_env import load_nifty_environment
load_nifty_environment()

from trading.one_direction_profit_lock_journal import KEY
from trading.strategy_records import StrategyEvent, StrategyRun, StrategyTrade

MIN_TRADES = 8
MIN_SESSIONS = 3
MIN_PROFIT_LOCKS = 2
MIN_REVERSALS = 1
OVERRIDE_KEY = "profit_lock_live_evidence_override"


def evidence_override(factory, user_id):
    """User-approved exception to evidence checks, stored in the Rails UI."""
    with factory() as db:
        value = db.execute(text("SELECT value FROM market_configs WHERE user_id=:user_id AND key=:key"),
                           {"user_id": user_id, "key": OVERRIDE_KEY}).scalar_one_or_none()
    if not value:
        return False
    try:
        approval = json.loads(value)
    except (TypeError, ValueError):
        return False
    return (approval.get("approved") is True and
            approval.get("acknowledgement") == "I accept live trading without the historical option backtest and verified reversal exit")


def forward_evidence(factory, user_id):
    with factory() as db:
        trades = list(db.scalars(select(StrategyTrade).where(
            StrategyTrade.user_id == user_id, StrategyTrade.strategy_key == KEY,
            StrategyTrade.mode == "paper", StrategyTrade.status == "closed",
            StrategyTrade.entry_premium.is_not(None), StrategyTrade.exit_premium.is_not(None),
            func.coalesce(StrategyTrade.error_message, "").not_like("Day-end paper index proxy;%"),
        )))
        # Keep only scalar values once the session commits.  ORM instances expire
        # on context exit, so using them below can raise DetachedInstanceError and
        # incorrectly block a valid live-promotion request.
        trade_data = [(trade.strategy_run_id, trade.trading_date) for trade in trades]
        run_ids = {run_id for run_id, _ in trade_data}
        events = list(db.scalars(select(StrategyEvent).where(StrategyEvent.strategy_run_id.in_(run_ids)))) if run_ids else []
        event_data = [(event.action, event.reason) for event in events]
    sessions = {trading_date for _, trading_date in trade_data}
    locks = sum(action == "PROFIT_LOCK_UPDATED" for action, _ in event_data)
    reversals = sum(reason == "reversal" and action.startswith("EXIT_")
                    for action, reason in event_data)
    complete = (len(trade_data) >= MIN_TRADES and len(sessions) >= MIN_SESSIONS
                and locks >= MIN_PROFIT_LOCKS and reversals >= MIN_REVERSALS)
    return {"complete": complete, "closed_option_trades": len(trade_data),
            "sessions": len(sessions), "profit_lock_events": locks, "reversal_exits": reversals,
            "requirements": {"trades": MIN_TRADES, "sessions": MIN_SESSIONS,
                             "profit_locks": MIN_PROFIT_LOCKS, "reversals": MIN_REVERSALS}}


def readiness(factory, user_id, option_report):
    path = Path(option_report)
    option = json.loads(path.read_text()) if path.exists() else {
        "status": "blocked_missing_historical_option_bid_ask", "accepted": False}
    forward = forward_evidence(factory, user_id)
    override = evidence_override(factory, user_id)
    paper_ready = (forward["closed_option_trades"] >= MIN_TRADES and
                   forward["sessions"] >= MIN_SESSIONS and
                   forward["profit_lock_events"] >= MIN_PROFIT_LOCKS)
    return {"ready": paper_ready and (override or (bool(option.get("accepted")) and
            forward["reversal_exits"] >= MIN_REVERSALS)),
            "evidence_override": override,
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
