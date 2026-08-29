"""
merge_trades.py — pure SQL version, no ORM.
"""

from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict
from decimal import Decimal

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn():
    url = re.sub(r"^postgresql\+[^:]+://", "postgresql://", DATABASE_URL)
    return psycopg2.connect(url)


def merge_pair(conn, entry_id, exit_id, update_params):
    with conn.cursor() as cur:
        try:
            cur.execute("SAVEPOINT merge_pair")

            # Verify both trades still exist before proceeding
            cur.execute(
                "SELECT id FROM trades WHERE id IN (%s, %s)",
                (entry_id, exit_id)
            )
            found = {r[0] for r in cur.fetchall()}
            if entry_id not in found:
                cur.execute("ROLLBACK TO SAVEPOINT merge_pair")
                return False, f"entry trade {entry_id} no longer exists (was merged in a prior step)"
            if exit_id not in found:
                cur.execute("ROLLBACK TO SAVEPOINT merge_pair")
                return False, f"exit trade {exit_id} no longer exists (was merged in a prior step)"

            # Step 1: move all order_events from exit trade → entry trade
            cur.execute(
                "UPDATE order_events SET trade_id = %s WHERE trade_id = %s",
                (entry_id, exit_id)
            )

            # Step 2: write P&L and exit details onto the entry trade
            cur.execute("""
                UPDATE trades SET
                    exit_price  = %(exit_price)s,
                    exit_value  = %(exit_value)s,
                    exited_at   = %(exited_at)s,
                    exit_reason = 'synced_from_csv',
                    pnl         = %(pnl)s,
                    pnl_pct     = %(pnl_pct)s,
                    quantity    = %(quantity)s,
                    status      = 'closed'
                WHERE id = %(entry_id)s
            """, update_params)

            # Step 3: delete the now-orphaned exit trade
            cur.execute("DELETE FROM positions WHERE trade_id = %s", (exit_id,))
            cur.execute("DELETE FROM trades WHERE id = %s", (exit_id,))

            cur.execute("RELEASE SAVEPOINT merge_pair")
            return True, None

        except Exception as exc:
            cur.execute("ROLLBACK TO SAVEPOINT merge_pair")
            return False, str(exc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    dry_run = args.dry_run

    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT
            t.id, t.instrument_id, t.direction, t.entry_price,
            t.quantity, t.entered_at, t.user_id,
            i.symbol
        FROM trades t
        JOIN instruments i ON i.id = t.instrument_id
        WHERE t.status = 'closed'
        ORDER BY t.entered_at
    """)
    rows = cur.fetchall()
    print(f"Total closed trades in DB: {len(rows)}\n")

    by_instrument: dict = defaultdict(lambda: {"BUY": [], "SELL": []})
    for row in rows:
        by_instrument[row["instrument_id"]][row["direction"]].append(dict(row))

    pairs: list[dict] = []
    stats = {"merged": 0, "skipped": 0, "errors": 0}

    print(
        f"{'Symbol':<28} {'Entry':<12} {'Exit':<12} "
        f"{'Entry ₹':>10} {'Exit ₹':>10} {'Qty':>8} {'P&L':>12}  Result"
    )
    print("-" * 105)

    for instrument_id, sides in by_instrument.items():
        buys  = list(sides["BUY"])
        sells = list(sides["SELL"])

        if not buys or not sells:
            stats["skipped"] += len(buys) + len(sells)
            continue

        symbol = (buys or sells)[0]["symbol"]

        while buys and sells:
            b = buys.pop(0)
            s = sells.pop(0)

            buy_qty  = Decimal(str(b["quantity"]))
            sell_qty = Decimal(str(s["quantity"]))
            matched  = min(buy_qty, sell_qty)

            buy_price  = Decimal(str(b["entry_price"]))
            sell_price = Decimal(str(s["entry_price"]))

            if b["entered_at"] <= s["entered_at"]:
                entry, exit_ = b, s
                pnl = (sell_price - buy_price) * matched
            else:
                entry, exit_ = s, b
                pnl = (sell_price - buy_price) * matched

            pnl    = pnl.quantize(Decimal("0.01"))
            result = "WIN " if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"

            entry_date = entry["entered_at"].strftime("%Y-%m-%d") if entry["entered_at"] else "—"
            exit_date  = exit_["entered_at"].strftime("%Y-%m-%d") if exit_["entered_at"] else "—"

            print(
                f"{symbol:<28} {entry_date:<12} {exit_date:<12} "
                f"₹{Decimal(str(entry['entry_price'])):>8.2f} "
                f"₹{Decimal(str(exit_['entry_price'])):>8.2f} "
                f"{matched:>8.0f} "
                f"{'₹'+str(pnl):>12}  {result}"
            )

            exit_price = Decimal(str(exit_["entry_price"]))
            exit_value = (exit_price * matched).quantize(Decimal("0.01"))
            entry_val  = Decimal(str(entry["entry_price"])) * matched
            pnl_pct    = (
                (pnl / entry_val * 100).quantize(Decimal("0.0001"))
                if entry_val else None
            )

            pairs.append({
                "entry_id": entry["id"],
                "exit_id":  exit_["id"],
                "update_params": {
                    "entry_id":   entry["id"],
                    "exit_price": float(exit_price),
                    "exit_value": float(exit_value),
                    "exited_at":  exit_["entered_at"],
                    "pnl":        float(pnl),
                    "pnl_pct":    float(pnl_pct) if pnl_pct else None,
                    "quantity":   float(matched),
                },
            })

            if buy_qty > sell_qty:
                b["quantity"] = float(buy_qty - sell_qty)
                buys.insert(0, b)
            elif sell_qty > buy_qty:
                s["quantity"] = float(sell_qty - buy_qty)
                sells.insert(0, s)

        stats["skipped"] += len(buys) + len(sells)

    if dry_run:
        print(f"\nDRY RUN — {len(pairs)} pairs identified, nothing written.")
    else:
        print(f"\nApplying {len(pairs)} merges...")

        # Track which trade IDs have been deleted so far this session.
        # If a planned entry_id was itself deleted as an exit_id in a prior
        # step, skip it rather than producing a FK violation.
        deleted_ids: set = set()

        for pair in pairs:
            entry_id = pair["entry_id"]
            exit_id  = pair["exit_id"]

            if entry_id in deleted_ids:
                print(f"  SKIP {entry_id} ← {exit_id}: entry was already consumed as an exit in a prior merge")
                stats["skipped"] += 1
                continue
            if exit_id in deleted_ids:
                print(f"  SKIP {entry_id} ← {exit_id}: exit was already consumed in a prior merge")
                stats["skipped"] += 1
                continue

            ok, err = merge_pair(conn, entry_id, exit_id, pair["update_params"])
            if ok:
                stats["merged"] += 1
                deleted_ids.add(exit_id)
            else:
                stats["errors"] += 1
                print(f"  ERROR merging {entry_id} ← {exit_id}: {err}")

        conn.commit()
        print("Committed.")

        cur.execute("""
            SELECT
                COUNT(*)                                        AS total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)      AS winners,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)      AS losers,
                ROUND(SUM(pnl)::numeric, 2)                    AS total_pnl
            FROM trades
            WHERE status = 'closed' AND pnl IS NOT NULL
        """)
        row = cur.fetchone()
        total    = row["total"] or 0
        winners  = int(row["winners"] or 0)
        losers   = int(row["losers"] or 0)
        win_rate = (winners / total * 100) if total else 0.0

        print(f"\n  Closed trades : {total}")
        print(f"  Winners       : {winners}")
        print(f"  Losers        : {losers}")
        print(f"  Win rate      : {win_rate:.1f}%")
        print(f"  Total P&L     : ₹{row['total_pnl']:,}")

    print("\n" + "=" * 60)
    print("MERGE COMPLETE")
    print(f"  Pairs identified : {len(pairs)}")
    if not dry_run:
        print(f"  Successfully merged : {stats['merged']}")
        print(f"  Errors              : {stats['errors']}")
    print(f"  Unmatched (open)    : {stats['skipped']}")
    print("=" * 60)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()