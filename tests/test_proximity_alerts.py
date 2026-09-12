from datetime import datetime, timedelta, timezone

from proximity_alerts import alert_is_due, proximity_condition


def test_explicit_zone_detects_near_price():
    condition = proximity_condition(
        key="support:1", label="Support", ltp=99.8, reference=100,
        buffer_pct=0.3, zone_lower=99.5, zone_upper=100.5,
    )
    assert condition["key"] == "support:1"


def test_percentage_buffer_rejects_distant_price():
    assert proximity_condition(
        key="ema:65", label="EMA 65", ltp=101, reference=100, buffer_pct=0.3,
    ) is None


def test_only_two_alerts_with_fifteen_minute_gap():
    now = datetime.now(timezone.utc)
    assert alert_is_due(now, 0, None)
    assert not alert_is_due(now, 1, now - timedelta(minutes=14))
    assert alert_is_due(now, 1, now - timedelta(minutes=15))
    assert not alert_is_due(now, 2, now - timedelta(days=1))
