from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select, update

from trading.database import get_db
from trading.models import Instrument, Trade
from trading.repositories import UserRepo

with get_db() as db:
    user = UserRepo.get_active(db)
    db.execute(update(Trade).where(Trade.status == "open").values(status="cancelled"))

    inst = db.scalar(select(Instrument).where(Instrument.instrument_token == 256265))

    # March 20 open was 23110 and ran to 23345 in first 30 mins
    # Enter at open, SL below first candle low, target realistic
    trade = Trade(
        instrument_id   = inst.id,
        user_id         = user.id,
        direction       = "BUY",
        trade_type      = "EQUITY",
        product         = "MIS",
        entry_price     = Decimal("23120.00"),  # near open
        quantity        = Decimal("1"),
        entry_value     = Decimal("23120.00"),
        initial_sl      = Decimal("23100.00"),  # tight SL — 20 pts
        current_sl      = Decimal("23100.00"),
        sl_distance_pct = Decimal("0.0865"),
        sl_method       = "r_multiple",
        target_price    = Decimal("23280.00"),  # 8R target — achievable given day high 23345
        risk_amount     = Decimal("20.00"),     # 20 pts risk
        reward_ratio    = Decimal("8.00"),
        status          = "open",
        entered_at      = datetime(2026, 3, 20, 3, 44, 0, tzinfo=timezone.utc),
    )
    db.add(trade)
    db.flush()
    print(f"Trade: BUY at ₹23120 | SL ₹23100 | Target ₹23280")
    print(f"Risk: ₹20 per unit | R:R 8:1")
    print(f"Day high was ₹23345 — target should be hit at candle ~16")
    print(f"\nRun: python replay.py")