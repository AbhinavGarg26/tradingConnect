from datetime import datetime, timedelta, timezone

from token_alert_schedule import due_alert_stage, next_alert_at, next_alert_label


NOW = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)


def test_alert_is_due_immediately():
    assert due_alert_stage(NOW, NOW, -1, None) == 0
    assert next_alert_label(0) == "5 minutes"


def test_alerts_follow_elapsed_escalation_schedule():
    started = NOW - timedelta(minutes=16)

    assert due_alert_stage(NOW, started, 1, NOW - timedelta(minutes=11)) == 2
    assert next_alert_label(2) == "30 minutes"


def test_missed_stages_collapse_to_latest_due_stage():
    started = NOW - timedelta(minutes=65)

    assert due_alert_stage(NOW, started, 0, NOW - timedelta(hours=1)) == 4


def test_final_stage_repeats_every_three_hours():
    started = NOW - timedelta(hours=8)

    assert due_alert_stage(NOW, started, 6, NOW - timedelta(hours=2)) is None
    assert due_alert_stage(NOW, started, 6, NOW - timedelta(hours=3)) == 6
    assert next_alert_at(NOW, started, 6) == NOW + timedelta(hours=3)
