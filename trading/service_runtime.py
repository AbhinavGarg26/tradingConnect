"""Database-backed health, token-state, and exception reporting for services."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import socket
import threading
import traceback
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from trading.database import get_db
from token_alert_schedule import due_alert_stage, next_alert_at, next_alert_label


class ServiceRuntimeMonitor:
    """Publish a service heartbeat without allowing monitoring failures to crash it."""

    def __init__(self, service_name: str, logger: logging.Logger, interval_seconds: int = 30):
        self.service_name = service_name
        self.logger = logger
        self.interval_seconds = interval_seconds
        self.user_id: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, activity: str = "Starting") -> None:
        self._publish(status="starting", activity=activity, started=True)
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"{self.service_name}-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def success(self, activity: str) -> None:
        self._publish(status="active", activity=activity, success=True)

    def degraded(self, activity: str) -> None:
        self._publish(status="degraded", activity=activity)

    def exception(self, exc: BaseException, activity: str) -> None:
        try:
            with get_db() as db:
                user_id = self._resolve_user_id(db)
                if user_id is None:
                    return
                status_id = self._upsert(db, user_id, "error", activity, error=True)
                message = self._redact(str(exc)[:10_000] or exc.__class__.__name__)
                trace = self._redact(
                    "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-50_000:]
                )
                fingerprint = hashlib.sha256(
                    f"{exc.__class__.__name__}:{message}".encode("utf-8", errors="replace")
                ).hexdigest()
                db.execute(text("""
                    INSERT INTO service_runtime_exceptions (
                        service_runtime_status_id, exception_class, message,
                        backtrace, activity, fingerprint, context, occurred_at,
                        created_at, updated_at
                    ) VALUES (
                        :status_id, :exception_class, :message,
                        :backtrace, :activity, :fingerprint, '{}'::jsonb, NOW(),
                        NOW(), NOW()
                    )
                """), {
                    "status_id": status_id,
                    "exception_class": exc.__class__.__name__,
                    "message": message,
                    "backtrace": trace,
                    "activity": activity,
                    "fingerprint": fingerprint,
                })
                # Retain only exceptions from the last seven days and at most
                # the newest ten for this service.
                db.execute(text("""
                    DELETE FROM service_runtime_exceptions
                    WHERE service_runtime_status_id = :status_id
                      AND occurred_at < NOW() - INTERVAL '7 days'
                """), {"status_id": status_id})
                db.execute(text("""
                    DELETE FROM service_runtime_exceptions
                    WHERE id IN (
                        SELECT id FROM service_runtime_exceptions
                        WHERE service_runtime_status_id = :status_id
                        ORDER BY occurred_at DESC
                        OFFSET 10
                    )
                """), {"status_id": status_id})
        except Exception as monitor_error:
            self.logger.warning("Runtime exception reporting unavailable: %s", monitor_error)

    def stop(self, activity: str = "Stopped") -> None:
        self._stop.set()
        self._publish(status="stopped", activity=activity)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._publish(
                status="active",
                activity="Waiting for scheduled work",
                heartbeat_only=True,
            )

    def _publish(
        self,
        *,
        status: str,
        activity: str,
        started: bool = False,
        success: bool = False,
        heartbeat_only: bool = False,
    ) -> None:
        try:
            with get_db() as db:
                user_id = self._resolve_user_id(db)
                if user_id is not None:
                    self._upsert(
                        db, user_id, status, activity,
                        started=started, success=success, heartbeat_only=heartbeat_only,
                    )
        except Exception as exc:
            self.logger.warning("Runtime heartbeat unavailable: %s", exc)

    def _resolve_user_id(self, db) -> Optional[int]:
        if self.user_id is None:
            self.user_id = db.execute(text(
                "SELECT id FROM users ORDER BY created_at ASC LIMIT 1"
            )).scalar()
        return self.user_id

    def _token_state(self, db, user_id: int) -> tuple[str, Optional[datetime]]:
        row = db.execute(text("""
            SELECT session_token_encrypted, session_expires_at
            FROM exchange_links
            WHERE user_id = :user_id AND is_active = TRUE
            LIMIT 1
        """), {"user_id": user_id}).mappings().first()
        if not row or not row["session_token_encrypted"]:
            return "missing", None
        expires_at = row["session_expires_at"]
        if expires_at is None:
            return "unknown", None
        now = datetime.now(timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        seconds_left = (expires_at - now).total_seconds()
        if seconds_left <= 0:
            return "expired", expires_at
        if seconds_left <= 30 * 60:
            return "expiring", expires_at
        return "valid", expires_at

    def _upsert(
        self,
        db,
        user_id: int,
        status: str,
        activity: str,
        *,
        started: bool = False,
        success: bool = False,
        error: bool = False,
        heartbeat_only: bool = False,
    ):
        token_status, token_expires_at = self._token_state(db, user_id)
        status_id = db.execute(text("""
            INSERT INTO service_runtime_statuses (
                user_id, service_name, status, pid, host_name,
                current_activity, last_heartbeat_at, last_started_at,
                last_success_at, last_error_at, token_status,
                token_expires_at, metadata, created_at, updated_at
            ) VALUES (
                :user_id, :service_name, :status, :pid, :host_name,
                :activity, NOW(), CASE WHEN :started THEN NOW() END,
                CASE WHEN :success THEN NOW() END, CASE WHEN :error THEN NOW() END,
                :token_status, :token_expires_at, '{}'::jsonb, NOW(), NOW()
            )
            ON CONFLICT (user_id, service_name) DO UPDATE SET
                status = CASE WHEN :heartbeat_only
                    THEN service_runtime_statuses.status ELSE EXCLUDED.status END,
                pid = EXCLUDED.pid,
                host_name = EXCLUDED.host_name,
                current_activity = CASE WHEN :heartbeat_only
                    THEN service_runtime_statuses.current_activity ELSE EXCLUDED.current_activity END,
                last_heartbeat_at = NOW(),
                last_started_at = CASE WHEN :started THEN NOW()
                    ELSE service_runtime_statuses.last_started_at END,
                last_success_at = CASE WHEN :success THEN NOW()
                    ELSE service_runtime_statuses.last_success_at END,
                last_error_at = CASE WHEN :error THEN NOW()
                    ELSE service_runtime_statuses.last_error_at END,
                token_status = EXCLUDED.token_status,
                token_expires_at = EXCLUDED.token_expires_at,
                updated_at = NOW()
            RETURNING id
        """), {
            "user_id": user_id,
            "service_name": self.service_name,
            "status": status,
            "pid": os.getpid(),
            "host_name": socket.gethostname(),
            "activity": activity,
            "started": started,
            "success": success,
            "error": error,
            "heartbeat_only": heartbeat_only,
            "token_status": token_status,
            "token_expires_at": token_expires_at,
        }).scalar_one()
        self._maybe_alert_invalid_token(db, user_id, token_status, token_expires_at)
        return status_id

    def _maybe_alert_invalid_token(
        self,
        db,
        user_id: int,
        token_status: str,
        token_expires_at: Optional[datetime],
    ) -> None:
        """Coordinate one persistent invalid-token reminder stream per user."""
        entity_key = str(user_id)
        if token_status not in {"expired", "missing"}:
            db.execute(text("""
                DELETE FROM market_live_state
                WHERE entity_type = 'USER' AND entity_key = :entity_key
                  AND metric_type = 'TOKEN_ALERT' AND metric_key = 'kite_session'
            """), {"entity_key": entity_key})
            return

        # market_breath and kite_market_fetcher heartbeat concurrently. A
        # transaction advisory lock ensures only one of them sends each stage.
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"kite-token-alert:{user_id}"},
        )
        state = db.execute(text("""
            SELECT payload
            FROM market_live_state
            WHERE entity_type = 'USER' AND entity_key = :entity_key
              AND metric_type = 'TOKEN_ALERT' AND metric_key = 'kite_session'
            FOR UPDATE
        """), {"entity_key": entity_key}).scalar()

        now = datetime.now(timezone.utc)
        state = state or {}
        invalid_since = self._parse_time(state.get("invalid_since")) or now
        last_alert_at = self._parse_time(state.get("last_alert_at"))
        last_stage = int(state.get("last_stage", -1))
        stage = due_alert_stage(now, invalid_since, last_stage, last_alert_at)
        if stage is None:
            return

        from trading.alerts import Alerter

        expiry = (
            token_expires_at.astimezone(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC")
            if token_expires_at else "Not available"
        )
        message = (
            "🚨 <b>Kite session token invalid</b>\n"
            f"Status: <b>{html.escape(token_status.title())}</b>\n"
            f"Expiry: {html.escape(expiry)}\n"
            f"Detected by: {html.escape(self.service_name)}\n"
            f"Next reminder: {next_alert_label(stage)}\n\n"
            "Refresh the Kite session from the Rails Token refresh page."
        )
        if not Alerter.from_db(db, user_id).send(message):
            self.logger.warning("Invalid Kite token alert could not be delivered")
            return

        payload = {
            "token_status": token_status,
            "token_expires_at": token_expires_at.isoformat() if token_expires_at else None,
            "invalid_since": invalid_since.isoformat(),
            "last_alert_at": now.isoformat(),
            "last_stage": stage,
            "next_alert": next_alert_label(stage),
            "next_alert_at": next_alert_at(now, invalid_since, stage).isoformat(),
            "last_sender": self.service_name,
        }
        db.execute(text("""
            INSERT INTO market_live_state (
                entity_type, entity_key, metric_type, metric_key,
                numeric_value, payload, event_time, is_complete,
                created_at, updated_at
            ) VALUES (
                'USER', :entity_key, 'TOKEN_ALERT', 'kite_session',
                :stage, CAST(:payload AS JSONB), :event_time, TRUE, NOW(), NOW()
            )
            ON CONFLICT (entity_type, entity_key, metric_type, metric_key)
            DO UPDATE SET
                numeric_value = EXCLUDED.numeric_value,
                payload = EXCLUDED.payload,
                event_time = EXCLUDED.event_time,
                is_complete = TRUE,
                updated_at = NOW()
        """), {
            "entity_key": entity_key,
            "stage": stage,
            "payload": json.dumps(payload),
            "event_time": now,
        })

    @staticmethod
    def _parse_time(value) -> Optional[datetime]:
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    @staticmethod
    def _redact(value: str) -> str:
        """Remove known credentials and common token assignments from diagnostics."""
        redacted = value
        for env_name in (
            "DATABASE_URL", "DB_ENCRYPTION_KEY", "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID", "KITE_ACCESS_TOKEN",
        ):
            secret = os.getenv(env_name)
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return re.sub(
            r"(?i)(access_token|api_secret|bot_token|authorization)(\s*[:=]\s*)[^\s,;]+",
            r"\1\2[REDACTED]",
            redacted,
        )
