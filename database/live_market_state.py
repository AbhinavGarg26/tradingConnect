"""Upsert helpers for the generic, mutable live market state table."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


TIMEFRAMES = (1, 3, 15)
HISTORICAL_INTERVALS = {1: "minute", 3: "3minute", 15: "15minute"}
INDIA_TZ = ZoneInfo("Asia/Kolkata")


def canonical_event_time(value: datetime | str) -> datetime:
    """Return one timezone-aware UTC value for stable candle identity."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=INDIA_TZ)
    return value.astimezone(timezone.utc)


def _candle_payload(candle: dict, timeframe: int, now: datetime) -> tuple[dict, datetime]:
    event_time = canonical_event_time(candle["date"])
    is_complete = event_time + timedelta(minutes=timeframe) <= now
    payload = {
        "instrument_token": candle.get("instrument_token"),
        "timeframe_minutes": timeframe,
        "open_time": event_time.isoformat(),
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "volume": int(candle.get("volume") or 0),
        "is_complete": is_complete,
    }
    if candle.get("oi") is not None:
        payload["oi"] = int(candle["oi"])
    return payload, event_time


def prune_inactive_position_risk(db: Session, active_position_keys: set[str]) -> None:
    if not active_position_keys:
        db.execute(text("""
            DELETE FROM market_live_state
            WHERE entity_type = 'POSITION' AND metric_type = 'RISK'
        """))
        return
    query = text("""
        DELETE FROM market_live_state
        WHERE entity_type = 'POSITION'
          AND metric_type = 'RISK'
          AND entity_key NOT IN :active_keys
    """).bindparams(bindparam("active_keys", expanding=True))
    db.execute(query, {"active_keys": sorted(active_position_keys)})


def upsert_live_metric(
    db: Session,
    *,
    entity_type: str,
    entity_key: str,
    metric_type: str,
    metric_key: str,
    payload: dict,
    numeric_value: float | None = None,
    timeframe_minutes: int | None = None,
    event_time: datetime | str | None = None,
    is_complete: bool | None = None,
) -> None:
    db.execute(text("""
        INSERT INTO market_live_state (
            entity_type, entity_key, metric_type, metric_key,
            timeframe_minutes, numeric_value, payload, event_time,
            is_complete, created_at, updated_at
        ) VALUES (
            :entity_type, :entity_key, :metric_type, :metric_key,
            :timeframe_minutes, :numeric_value, CAST(:payload AS JSONB), :event_time,
            :is_complete, NOW(), NOW()
        )
        ON CONFLICT (entity_type, entity_key, metric_type, metric_key)
        DO UPDATE SET
            timeframe_minutes = EXCLUDED.timeframe_minutes,
            numeric_value = EXCLUDED.numeric_value,
            payload = EXCLUDED.payload,
            event_time = EXCLUDED.event_time,
            is_complete = EXCLUDED.is_complete,
            updated_at = NOW()
    """), {
        "entity_type": entity_type,
        "entity_key": entity_key,
        "metric_type": metric_type,
        "metric_key": metric_key,
        "timeframe_minutes": timeframe_minutes,
        "numeric_value": numeric_value,
        "payload": json.dumps(payload, default=str),
        "event_time": event_time,
        "is_complete": is_complete,
    })


def sync_instrument_live_state(
    db: Session,
    price_stream,
    *,
    entity_key: str,
    instrument_token: int,
) -> None:
    ltp = price_stream.get_price(instrument_token, max_age_seconds=10)
    if ltp is not None:
        upsert_live_metric(
            db,
            entity_type="INSTRUMENT",
            entity_key=entity_key,
            metric_type="LTP",
            metric_key="latest",
            numeric_value=ltp,
            payload={"ltp": ltp, "instrument_token": instrument_token},
            event_time=datetime.now(timezone.utc),
            is_complete=True,
        )

    for timeframe in TIMEFRAMES:
        candles = price_stream.candle_snapshots(instrument_token, timeframe)
        for candle in candles:
            open_time = canonical_event_time(candle["open_time"])
            candle = {**candle, "open_time": open_time.isoformat()}
            upsert_live_metric(
                db,
                entity_type="INSTRUMENT",
                entity_key=entity_key,
                metric_type="CANDLE",
                metric_key=f"{timeframe}m:{open_time.isoformat()}",
                timeframe_minutes=timeframe,
                numeric_value=candle["close"],
                payload=candle,
                event_time=open_time,
                is_complete=bool(candle["is_complete"]),
            )

        db.execute(text("""
            DELETE FROM market_live_state
            WHERE id IN (
                SELECT id
                FROM market_live_state
                WHERE entity_type = 'INSTRUMENT'
                  AND entity_key = :entity_key
                  AND metric_type = 'CANDLE'
                  AND timeframe_minutes = :timeframe
                ORDER BY event_time DESC
                OFFSET 5
            )
        """), {"entity_key": entity_key, "timeframe": timeframe})


def bootstrap_instrument_candles(
    kite,
    db: Session,
    *,
    entity_key: str,
    instrument_token: int,
    lookback_days: int = 10,
) -> list[dict]:
    """Seed the five latest candles so a service restart has immediate context."""
    now = datetime.now(timezone.utc)
    from_time = now - timedelta(days=lookback_days)
    unfinished: list[dict] = []

    for timeframe, interval in HISTORICAL_INTERVALS.items():
        candles = kite.historical_data(
            instrument_token,
            from_time,
            now,
            interval,
            continuous=False,
            oi=False,
        )
        for raw_candle in candles[-5:]:
            candle = {**raw_candle, "instrument_token": instrument_token}
            payload, event_time = _candle_payload(candle, timeframe, now)
            upsert_live_metric(
                db,
                entity_type="INSTRUMENT",
                entity_key=entity_key,
                metric_type="CANDLE",
                metric_key=f"{timeframe}m:{event_time.isoformat()}",
                timeframe_minutes=timeframe,
                numeric_value=payload["close"],
                payload=payload,
                event_time=event_time,
                is_complete=payload["is_complete"],
            )
            if not payload["is_complete"]:
                unfinished.append(payload)

    return unfinished


def sync_position_risk_state(
    db: Session,
    *,
    position: dict,
    ltp: float,
    buy_price: float,
    pnl_pct: float,
    soft_loss_pct: float,
    stop_state: dict,
    exit_reason: str | None,
) -> None:
    entity_key = ":".join([
        str(position["exchange"]),
        str(position["tradingsymbol"]),
        str(position["product"]),
    ])
    payload = {
        "tradingsymbol": position["tradingsymbol"],
        "exchange": position["exchange"],
        "product": position["product"],
        "instrument_token": int(position["instrument_token"]),
        "quantity": int(position["quantity"]),
        "buy_price": buy_price,
        "ltp": ltp,
        "current_pnl_pct": pnl_pct,
        "soft_stop_pct": -soft_loss_pct,
        "emergency_stop_pct": -(soft_loss_pct + 2.0),
        "exit_reason": exit_reason,
        **stop_state,
    }
    upsert_live_metric(
        db,
        entity_type="POSITION",
        entity_key=entity_key,
        metric_type="RISK",
        metric_key="latest",
        numeric_value=stop_state["worst_pnl_pct"],
        payload=payload,
        event_time=datetime.now(timezone.utc),
        is_complete=True,
    )
