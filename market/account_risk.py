"""Account-level funds, drawdown, loss-streak and trade-stat monitoring."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, time
import json
import logging
import time as monotonic_time
from zoneinfo import ZoneInfo

from sqlalchemy import text

from database.live_market_state import upsert_live_metric


IST = ZoneInfo("Asia/Kolkata")
SOFT_LOSS_PCT = 4.0
HARD_LOSS_PCT = 8.0
PROFIT_GIVEBACK_RATIO = 0.50
SOFT_REMINDER = timedelta(hours=1)
HARD_REMINDER = timedelta(minutes=15)
LOSS_STREAK_COOLDOWN = timedelta(minutes=30)
# Four concise decision-point summaries during the cash-market session.  The
# same rhythm is used by the Trading Discipline page so the report is useful
# before the next trade decision, rather than only after the close.
SUMMARY_CHECKPOINTS = (
    ("10:00", time(10, 0)),
    ("11:00", time(11, 0)),
    ("13:00", time(13, 0)),
    ("15:00", time(15, 0)),
)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def account_funds_snapshot(margins: dict) -> dict:
    """Normalize Kite's all-segment or equity-only margins response."""
    equity = margins.get("equity", margins)
    available = equity.get("available") or {}
    utilised = equity.get("utilised") or {}
    opening_balance = float(available.get("opening_balance") or 0)
    collateral = float(available.get("collateral") or 0)
    total_funds = opening_balance + collateral
    realised = float(utilised.get("m2m_realised") or 0)
    unrealised = float(utilised.get("m2m_unrealised") or 0)
    pnl = realised + unrealised
    loss_pct = abs(min(pnl, 0.0)) / total_funds * 100 if total_funds > 0 else 0.0
    return {
        "opening_balance": opening_balance,
        "collateral": collateral,
        "total_funds": total_funds,
        "available_balance": float(available.get("live_balance") or equity.get("net") or 0),
        "realised_pnl": realised,
        "unrealised_pnl": unrealised,
        "current_pnl": pnl,
        "loss_pct": loss_pct,
    }


def loss_level(loss_pct: float) -> str:
    if loss_pct >= HARD_LOSS_PCT:
        return "hard"
    if loss_pct >= SOFT_LOSS_PCT:
        return "soft"
    return "normal"


def _net_pnl(row: dict) -> float:
    return float(row.get("realized_pnl") or 0) - float(row.get("total_charges") or 0)


def calculate_trade_stats(rows: list[dict]) -> dict:
    closed = [row for row in rows if str(row.get("status", "")).upper() == "CLOSED"]
    wins = [row for row in closed if _net_pnl(row) > 0]
    losses = [row for row in closed if _net_pnl(row) < 0]
    gross_profit = sum(_net_pnl(row) for row in wins)
    gross_loss = abs(sum(_net_pnl(row) for row in losses))

    side_stats = {}
    for side in ("CE", "PE"):
        side_rows = [row for row in closed if row.get("option_type") == side]
        side_wins = sum(1 for row in side_rows if _net_pnl(row) > 0)
        side_losses = sum(1 for row in side_rows if _net_pnl(row) < 0)
        side_stats[side] = {
            "trades": len(side_rows),
            "wins": side_wins,
            "losses": side_losses,
            "pnl": round(sum(_net_pnl(row) for row in side_rows), 2),
            "win_rate": round(side_wins / len(side_rows) * 100, 1) if side_rows else 0.0,
        }

    symbol_sides = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for row in closed:
        key = f"{row.get('symbol') or row.get('tradingsymbol')} {row.get('option_type') or 'OTHER'}"
        pnl = _net_pnl(row)
        item = symbol_sides[key]
        item["trades"] += 1
        item["wins"] += int(pnl > 0)
        item["losses"] += int(pnl < 0)
        item["pnl"] += pnl

    total = len(closed)
    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": total - len(wins) - len(losses),
        "win_rate": round(len(wins) / total * 100, 1) if total else 0.0,
        "net_pnl": round(sum(_net_pnl(row) for row in closed), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "ce": side_stats["CE"],
        "pe": side_stats["PE"],
        "symbol_sides": {
            key: {**value, "pnl": round(value["pnl"], 2)}
            for key, value in sorted(symbol_sides.items())
        },
    }


def latest_loss_pair_signature(rows: list[dict]) -> str | None:
    closed = sorted(
        (row for row in rows if str(row.get("status", "")).upper() == "CLOSED"),
        key=lambda row: row.get("exit_time") or datetime.min,
    )
    if len(closed) < 2 or not all(_net_pnl(row) < 0 for row in closed[-2:]):
        return None
    return ":".join(str(row.get("exit_order_id") or row.get("id") or "") for row in closed[-2:])


def performance_edge(stats: dict) -> str:
    ce, pe = stats["ce"], stats["pe"]
    eligible = [("CE", ce), ("PE", pe)]
    eligible = [(name, item) for name, item in eligible if item["trades"] >= 3]
    if not eligible:
        return "No reliable CE/PE edge yet—sample size is below 3 closed trades per side."
    winner, best = max(eligible, key=lambda pair: (pair[1]["pnl"], pair[1]["win_rate"]))
    other = pe if winner == "CE" else ce
    if best["pnl"] <= 0 or best["pnl"] <= other["pnl"]:
        return "No positive CE/PE edge today. Reduce size and wait for a clearer setup."
    return (
        f"Observed edge: {winner} has performed better today "
        f"({best['win_rate']:.1f}% wins, ₹{best['pnl']:,.0f} net). "
        "This is retrospective evidence, not a prediction—only trade a valid setup."
    )


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)


def _due(last_sent, interval: timedelta, now: datetime) -> bool:
    parsed = _parse_time(last_sent)
    return parsed is None or now - parsed >= interval


def due_summary_checkpoint(now: datetime, sent_checkpoints: list[str]) -> str | None:
    """Return the latest unsent in-session summary checkpoint, if any."""
    due = [key for key, checkpoint_time in SUMMARY_CHECKPOINTS if now.time() >= checkpoint_time]
    if not due:
        return None
    latest = due[-1]
    return latest if latest not in sent_checkpoints else None


class AccountRiskMonitor:
    def __init__(self, user_id, logger: logging.Logger, interval_seconds: float = 60.0):
        self.user_id = user_id
        self.logger = logger
        self.interval_seconds = interval_seconds
        self._last_run = 0.0

    def run_if_due(self, kite, db, now: datetime | None = None) -> None:
        monotonic_now = monotonic_time.monotonic()
        if monotonic_now - self._last_run < self.interval_seconds:
            return
        self._last_run = monotonic_now
        now = now or datetime.now(IST)

        try:
            if not self._market_session_active(db, now):
                return
            funds = account_funds_snapshot(kite.margins("equity"))
            if funds["total_funds"] <= 0:
                self.logger.warning("Account risk check skipped: total funds are unavailable")
                return
            rows = self._today_trades(db, now)
            stats = calculate_trade_stats(rows)
            state = self._load_state(db, now)
            self._evaluate_and_alert(db, funds, stats, rows, state, now)
        except Exception as exc:
            self.logger.exception("Account risk monitoring failed: %s", exc)

    def _today_trades(self, db, now: datetime) -> list[dict]:
        return [dict(row) for row in db.execute(text("""
            SELECT id, symbol, tradingsymbol, option_type, status,
                   realized_pnl, total_charges, exit_order_id, exit_time
              FROM market_trades
             WHERE entry_time >= :day_start
             ORDER BY COALESCE(exit_time, entry_time), id
        """), {"day_start": datetime.combine(now.date(), time.min)}).mappings()]

    def _load_state(self, db, now: datetime) -> dict:
        payload = db.execute(text("""
            SELECT payload FROM market_live_state
             WHERE entity_type = 'ACCOUNT' AND entity_key = :entity_key
               AND metric_type = 'RISK' AND metric_key = 'daily'
        """), {"entity_key": str(self.user_id)}).scalar()
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict) or payload.get("trade_date") != now.date().isoformat():
            return {"trade_date": now.date().isoformat(), "peak_pnl": 0.0, "risk_level": "normal"}
        return payload

    @staticmethod
    def _market_session_active(db, now: datetime) -> bool:
        """Avoid account alerts when NSE is shut for a weekend or holiday."""
        if now.weekday() >= 5 or not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
            return False
        holiday = db.execute(text("""
            SELECT 1 FROM trading_holidays WHERE holiday_date = :holiday_date LIMIT 1
        """), {"holiday_date": now.date()}).scalar()
        return holiday is None

    def _evaluate_and_alert(self, db, funds, stats, rows, state, now) -> None:
        # Import after market_breath has loaded .env; trading package model setup
        # requires DB_ENCRYPTION_KEY during import.
        from trading.alerts import Alerter
        alerter = Alerter.from_db(db, self.user_id)
        current_level = loss_level(funds["loss_pct"])
        previous_level = state.get("risk_level", "normal")
        state["peak_pnl"] = max(float(state.get("peak_pnl") or 0), funds["current_pnl"])

        if current_level == "hard" and (
            previous_level != "hard" or _due(state.get("last_hard_alert_at"), HARD_REMINDER, now)
        ):
            alerter.send_async(self._loss_message("hard", funds, stats))
            state["last_hard_alert_at"] = now.isoformat()
        elif current_level == "soft" and previous_level == "normal":
            alerter.send_async(self._loss_message("soft", funds, stats))
            state["last_soft_alert_at"] = now.isoformat()
        elif current_level == "soft" and _due(state.get("last_soft_alert_at"), SOFT_REMINDER, now):
            alerter.send_async(self._loss_message("soft", funds, stats, reminder=True))
            state["last_soft_alert_at"] = now.isoformat()
        elif current_level == "normal" and previous_level in {"soft", "hard"}:
            alerter.send_async(
                f"✅ <b>Account loss recovered below {SOFT_LOSS_PCT:.0f}%</b>\n"
                f"Current session P&amp;L: ₹{funds['current_pnl']:,.0f}. Risk is back inside the normal band."
            )

        peak = state["peak_pnl"]
        giveback = peak > 0 and funds["current_pnl"] < peak * PROFIT_GIVEBACK_RATIO
        if giveback and not state.get("profit_giveback_alerted"):
            alerter.send_async(
                "⚠️ <b>Profit Giveback Alert</b>\n"
                f"Session peak: ₹{peak:,.0f}\nCurrent P&amp;L: ₹{funds['current_pnl']:,.0f}\n"
                f"Giveback: {(peak - funds['current_pnl']) / peak * 100:.1f}%\n"
                "More than half of the session profit has been surrendered. Consider protecting remaining gains."
            )
            state["profit_giveback_alerted"] = True

        pair_signature = latest_loss_pair_signature(rows)
        if pair_signature and pair_signature != state.get("last_loss_pair_signature"):
            if _due(state.get("last_loss_streak_alert_at"), LOSS_STREAK_COOLDOWN, now):
                alerter.send_async(
                    "🟠 <b>Two Consecutive Losing Trades</b>\n"
                    "Pause before the next entry. Recheck setup quality, direction, size, and market regime.\n"
                    "Tomorrow offers another opportunity—protecting capital today keeps you ready for it."
                )
                state["last_loss_streak_alert_at"] = now.isoformat()
            state["last_loss_pair_signature"] = pair_signature

        sent_checkpoints = list(state.get("summary_sent_checkpoints") or [])
        checkpoint = due_summary_checkpoint(now, sent_checkpoints)
        if checkpoint:
            alerter.send_async(self._summary_message(funds, stats, checkpoint))
            sent_checkpoints.append(checkpoint)
        state["summary_sent_checkpoints"] = sent_checkpoints

        state.update({"risk_level": current_level, "funds": funds, "stats": stats, "updated_at": now.isoformat()})
        upsert_live_metric(
            db, entity_type="ACCOUNT", entity_key=str(self.user_id), metric_type="RISK",
            metric_key="daily", numeric_value=funds["loss_pct"], payload=state,
            event_time=now, is_complete=False,
        )
        db.commit()

    def _loss_message(self, level, funds, stats, reminder=False) -> str:
        if level == "hard":
            title = "🚨🚨 <b>HARD LOSS BREACH — STOP NEW TRADES</b>"
            action = "Loss is at or above 8% of total funds. Review and reduce risk immediately."
        else:
            title = "⚠️ <b>Soft Loss Breach</b>" + (" — reminder" if reminder else "")
            action = "Loss is above 4% of total funds. Reduce size and avoid revenge trading."
        return (
            f"{title}\n{action}\n\n"
            f"Total funds: ₹{funds['total_funds']:,.0f}\n"
            f"Current P&amp;L: ₹{funds['current_pnl']:,.0f} ({funds['loss_pct']:.2f}% loss)\n"
            f"Trades: {stats['total_trades']} | Wins: {stats['wins']} | Losses: {stats['losses']}\n"
            "Tomorrow offers another opportunity—protect capital and return with a clear plan."
        )

    def _summary_message(self, funds, stats, checkpoint: str) -> str:
        symbol_lines = sorted(
            stats["symbol_sides"].items(), key=lambda item: abs(item[1]["pnl"]), reverse=True
        )[:8]
        details = "\n".join(
            f"• {name}: {item['wins']}W/{item['losses']}L | ₹{item['pnl']:,.0f}"
            for name, item in symbol_lines
        ) or "• No closed trades"
        pf = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "—"
        return (
            f"📊 <b>Kite Risk &amp; Trade Summary — {checkpoint} IST</b>\n"
            f"Total funds: ₹{funds['total_funds']:,.0f} | Available: ₹{funds['available_balance']:,.0f}\n"
            f"Session P&amp;L: ₹{funds['current_pnl']:,.0f}\n"
            f"Closed trades: {stats['total_trades']} | {stats['wins']}W/{stats['losses']}L | Win rate: {stats['win_rate']:.1f}%\n"
            f"Net after charges: ₹{stats['net_pnl']:,.0f} | Profit factor: {pf}\n"
            f"CE: {stats['ce']['wins']}W/{stats['ce']['losses']}L | ₹{stats['ce']['pnl']:,.0f}\n"
            f"PE: {stats['pe']['wins']}W/{stats['pe']['losses']}L | ₹{stats['pe']['pnl']:,.0f}\n\n"
            f"<b>Symbol + side</b>\n{details}\n\n"
            f"{performance_edge(stats)}"
        )
