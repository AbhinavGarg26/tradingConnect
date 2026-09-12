"""Database-backed health, token-state, and exception reporting for services."""

from __future__ import annotations

import hashlib
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
        return db.execute(text("""
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
