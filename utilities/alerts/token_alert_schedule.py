"""Pure escalation schedule calculations for invalid Kite-token alerts."""

from datetime import datetime, timedelta
from typing import Optional


ALERT_OFFSETS_SECONDS = (0, 5 * 60, 15 * 60, 30 * 60, 60 * 60, 2 * 60 * 60, 3 * 60 * 60)
REPEAT_SECONDS = 3 * 60 * 60


def due_alert_stage(
    now: datetime,
    invalid_since: datetime,
    last_stage: int,
    last_alert_at: Optional[datetime],
) -> Optional[int]:
    """Return the due escalation stage, skipping missed stages without bursting."""
    elapsed = max(0, (now - invalid_since).total_seconds())
    due_stages = [
        index for index, offset in enumerate(ALERT_OFFSETS_SECONDS)
        if elapsed >= offset and index > last_stage
    ]
    if due_stages:
        return max(due_stages)

    final_stage = len(ALERT_OFFSETS_SECONDS) - 1
    if last_stage >= final_stage and last_alert_at:
        if now - last_alert_at >= timedelta(seconds=REPEAT_SECONDS):
            return final_stage
    return None


def next_alert_label(stage: int) -> str:
    labels = ("5 minutes", "15 minutes", "30 minutes", "1 hour", "2 hours", "3 hours")
    return labels[stage] if stage < len(labels) else "3 hours"


def next_alert_at(now: datetime, invalid_since: datetime, stage: int) -> datetime:
    next_stage = stage + 1
    if next_stage < len(ALERT_OFFSETS_SECONDS):
        return invalid_since + timedelta(seconds=ALERT_OFFSETS_SECONDS[next_stage])
    return now + timedelta(seconds=REPEAT_SECONDS)
