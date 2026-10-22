from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from nifty_15m_env import load_nifty_environment

load_nifty_environment()

from nifty_15m_live import place_broker_stop
import strategy_guardian
from trading.strategy_risk import RiskBlocked, stop_limit_prices


def test_stop_limit_uses_option_premium_and_exchange_tick():
    trigger, limit = stop_limit_prices(Decimal("100"), Decimal("1000"), 65, Decimal("0.05"))
    assert trigger == Decimal("84.65")
    assert limit == Decimal("82.10")
    assert (Decimal("100") - trigger) * 65 <= Decimal("1000")


def test_unprotectable_option_is_blocked_before_order():
    with pytest.raises(RiskBlocked):
        stop_limit_prices(Decimal("10"), Decimal("1000"), 65, Decimal("0.05"))


def test_broker_stop_is_sell_stop_limit_not_slm(monkeypatch):
    orders = []

    class FakeKite:
        VARIETY_REGULAR = "regular"
        TRANSACTION_TYPE_SELL = "SELL"
        PRODUCT_MIS = "MIS"
        ORDER_TYPE_SL = "SL"
        VALIDITY_DAY = "DAY"

        def place_order(self, **kwargs):
            orders.append(kwargs)
            return "protect-123"

        def order_history(self, order_id):
            return [{"status": "TRIGGER PENDING"}]

    recorded = []
    recorder = SimpleNamespace(protection_submitted=lambda *args: recorded.append(args))
    contract = {"tradingsymbol": "NIFTYTESTCE", "tick_size": 0.05}
    limits = SimpleNamespace(risk_per_trade=Decimal("1000"),
                             stop_limit_buffer_pct=Decimal("3"))
    assert place_broker_stop(FakeKite(), contract, 65, Decimal("100"), limits, recorder) == "protect-123"
    assert orders[0]["order_type"] == "SL"
    assert orders[0]["transaction_type"] == "SELL"
    assert recorded[0][0] == "protect-123"


def test_cancel_protection_waits_for_broker_confirmation():
    class FakeKite:
        VARIETY_REGULAR = "regular"

        def __init__(self):
            self.cancelled = False

        def order_history(self, order_id):
            return [{"status": "CANCELLED" if self.cancelled else "TRIGGER PENDING"}]

        def cancel_order(self, **kwargs):
            self.cancelled = True

    kite = FakeKite()
    assert strategy_guardian.cancel_protection(
        kite, SimpleNamespace(protection_order_id="protect-123")) is None
    assert kite.cancelled


def test_guardian_does_not_send_duplicate_exit_when_trade_reserved(monkeypatch):
    monkeypatch.setattr(strategy_guardian, "reserve_exit", lambda *args, **kwargs: False)
    class NeverUseKite:
        def __getattr__(self, name):
            raise AssertionError(f"broker call {name} would duplicate an exit")

    assert strategy_guardian.emergency_exit(
        NeverUseKite(), SimpleNamespace(id=7), "hard_risk_per_trade") is None


def test_disabled_strategy_still_monitors_and_exits_open_trade(monkeypatch):
    setting = SimpleNamespace(user_id=42, strategy_key="nifty_15m_breakout",
                              live_enabled=False, stop_mode="broker_stop_limit",
                              risk_per_trade=Decimal("500"), max_daily_loss=Decimal("4500"),
                              adverse_action="emergency_exit")
    trade = SimpleNamespace(id=7, user_id=42, strategy_key=setting.strategy_key,
                            status="open", option_symbol="NIFTYTESTPE", quantity=65,
                            entry_premium=Decimal("100"), protection_order_id="sl-7",
                            protection_trigger=Decimal("92"), trading_date=datetime.now().date())

    class FakeDb:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def scalars(self, query):
            entity = query.column_descriptions[0]["entity"]
            return [setting] if entity.__name__ == "StrategyRiskSetting" else [trade]

        def expunge_all(self):
            pass

        def scalar(self, _query):
            return Decimal(0)

    monkeypatch.setattr(strategy_guardian, "get_db", FakeDb)
    monkeypatch.setattr(strategy_guardian, "heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(strategy_guardian, "order_last", lambda *_args: {"status": "TRIGGER PENDING"})
    exits = []
    monkeypatch.setattr(strategy_guardian, "emergency_exit",
                        lambda _kite, _trade, reason, bid: exits.append((reason, bid)))

    class FakeKite:
        def quote(self, _symbols):
            return {"NFO:NIFTYTESTPE": {"depth": {"buy": [{"price": 90}]},
                                         "timestamp": datetime.now(timezone.utc)}}

    strategy_guardian.monitor_cycle(FakeKite(), 42)
    assert exits == [("hard_risk_per_trade", Decimal("90"))]
