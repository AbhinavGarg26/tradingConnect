from datetime import date, timedelta

from market.option_snapshots import select_contracts


def test_weekly_and_monthly_selection_preserves_roles_when_expiry_overlaps():
    today = date(2026, 11, 23)
    expiries = [today + timedelta(days=3), today + timedelta(days=10),
                today + timedelta(days=17), today + timedelta(days=24)]
    master = [{"name": "NIFTY", "exchange": "NFO", "segment": "NFO-OPT",
               "tradingsymbol": f"NIFTY{expiry:%y%m%d}{strike}{kind}",
               "instrument_type": kind, "expiry": expiry, "strike": strike,
               "instrument_token": i * 1000 + strike + (kind == "PE"),
               "lot_size": 65, "tick_size": .05}
              for i, expiry in enumerate(expiries)
              for strike in range(26000, 26501, 50) for kind in ("CE", "PE")]
    selected = select_contracts(master, 26242, today)
    assert len(selected) == 30
    for role in ("nearest_weekly", "next_weekly", "monthly"):
        rows = [(atm, row) for selected_role, atm, row in selected if selected_role == role]
        assert len(rows) == 10
        assert {row["strike_price"] for _, row in rows} == {26150, 26200, 26250, 26300, 26350}
        assert {atm for atm, _ in rows} == {26250}
    assert {row["expiry_date"] for role, _, row in selected if role == "monthly"} == {expiries[0]}
