"""Read-only broker summaries; missing broker data is never treated as zero."""
from html import escape
import httpx
from sqlalchemy import text


def groww_snapshot(db, user_id):
    from trading.exchange_link import ExchangeLinkRepo
    link = ExchangeLinkRepo.get_for_user(db, user_id, provider="groww")
    if not link or not link.is_session_valid:
        return None
    token = link.decrypt_session_token(db)
    rows = []
    for segment in ("CASH", "FNO"):
        response = httpx.get("https://api.groww.in/v1/positions/user",
            params={"segment": segment},
            headers={"Authorization": f"Bearer {token}", "X-API-VERSION": "1.0"}, timeout=5)
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "SUCCESS":
            return None
        positions = body.get("payload", {}).get("positions")
        if not isinstance(positions, list):
            return None
        rows.extend(positions)
    realised = unrealised = 0.0
    for row in rows:
        realized = next((row[k] for k in ("realised_pnl", "realized_pnl", "realized") if row.get(k) is not None), None)
        if realized is None:
            return None
        realised += float(realized)
        if float(row.get("quantity") or row.get("net_quantity") or 0):
            unrealized = next((row[k] for k in ("unrealised_pnl", "unrealized_pnl") if row.get(k) is not None), None)
            if unrealized is None:
                return None
            unrealised += float(unrealized)
    return {"realised_pnl": realised, "unrealised_pnl": unrealised,
            "current_pnl": realised + unrealised}


def portfolio_summary(db, user_id, funds, now, checkpoint):
    try:
        groww = groww_snapshot(db, user_id)
    except Exception:
        groww = None
    lines = [f"📊 <b>Portfolio summary — {escape(checkpoint)} IST</b>",
             now.strftime("%d %b %Y, %H:%M IST"), "",
             f"<b>Kite</b>: ₹{funds['current_pnl']:,.2f} session P&amp;L",
             f"Realised ₹{funds['realised_pnl']:,.2f} | Unrealised ₹{funds['unrealised_pnl']:,.2f}",
             f"Funds ₹{funds['total_funds']:,.2f} | Available ₹{funds['available_balance']:,.2f}"]
    if groww is None:
        lines += ["<b>Groww</b>: live P&amp;L unavailable or incomplete",
                  "<b>Combined live P&amp;L</b>: unavailable (Groww incomplete)"]
    else:
        lines += [f"<b>Groww</b>: ₹{groww['current_pnl']:,.2f} session P&amp;L",
                  f"Realised ₹{groww['realised_pnl']:,.2f} | Unrealised ₹{groww['unrealised_pnl']:,.2f}",
                  f"<b>Combined live P&amp;L</b>: ₹{funds['current_pnl'] + groww['current_pnl']:,.2f}"]
    rows = db.execute(text("""
        SELECT el.provider, COUNT(*) AS trades,
               COUNT(*) FILTER (WHERE mt.realized_pnl - COALESCE(mt.total_charges,0) > 0) AS wins,
               COALESCE(SUM(mt.realized_pnl - COALESCE(mt.total_charges,0)),0) AS pnl
          FROM market_trades mt JOIN exchange_links el ON el.id = mt.exchange_link_id
         WHERE mt.user_id = :user_id AND el.user_id = :user_id
           AND el.provider IN ('zerodha','groww') AND mt.status = 'CLOSED'
           AND mt.exit_time >= :day_start AND mt.exit_time <= :now
         GROUP BY el.provider
    """), {"user_id": user_id, "day_start": now.replace(hour=0, minute=0, second=0, microsecond=0), "now": now}).mappings()
    lines += ["", "<b>Synced closed trades today (net of recorded charges)</b>"]
    by_provider = {row['provider']: row for row in rows}
    total = 0.0
    for provider, label in (("zerodha", "Kite"), ("groww", "Groww")):
        row = by_provider.get(provider)
        if row is None:
            lines.append(f"{label}: no synced closed trades")
        else:
            total += float(row['pnl'])
            lines.append(f"{label}: {row['trades']} trades · {row['wins']} wins · ₹{float(row['pnl']):,.2f}")
    lines += [f"Combined synced realised net: ₹{total:,.2f}",
              "Live broker figures exclude unreported fees; synced totals depend on journal completeness."]
    return "\n".join(lines)
