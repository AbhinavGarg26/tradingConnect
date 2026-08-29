"""
exchange_link.py — broker connection model.

Table name:    exchange_links
Encrypted cols: access_id_encrypted, access_secret_encrypted, session_token_encrypted
Encryption:    pgcrypto pgp_sym_encrypt / pgp_sym_decrypt
Key source:    ENV["DB_ENCRYPTION_KEY"]

Usage:
    from trading.exchange_link import ExchangeLinkRepo

    with get_db() as db:
        link = ExchangeLinkRepo.get_for_user(db, user_id)
        kite = ExchangeLinkRepo.get_kite_client(db, user_id)
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from kiteconnect import KiteConnect
from sqlalchemy import Boolean, DateTime, ForeignKey, LargeBinary, String, select, text, BigInteger
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, Session

from trading.database import Base

_ENCRYPTION_KEY = os.environ.get("DB_ENCRYPTION_KEY")
if not _ENCRYPTION_KEY:
    raise RuntimeError("DB_ENCRYPTION_KEY environment variable is not set")


def _next_6am_utc() -> datetime:
    """Next 6:00 AM IST expressed as UTC (IST = UTC+5:30)."""
    ist_offset = timedelta(hours=5, minutes=30)
    now_ist    = datetime.now(timezone.utc) + ist_offset
    target_ist = now_ist.replace(hour=6, minute=0, second=0, microsecond=0)
    if target_ist <= now_ist:
        target_ist += timedelta(days=1)
    return target_ist - ist_offset


# ─────────────────────────────────────────────────────────────
# ExchangeLink
# ─────────────────────────────────────────────────────────────

class ExchangeLink(Base):
    __tablename__ = "exchange_links"

    id:                        Mapped[uuid.UUID]        = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id:                   Mapped[int]              = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False, unique=True)

    provider:                  Mapped[str]              = mapped_column(String(30), nullable=False, default="zerodha")
    account_ref:               Mapped[Optional[str]]    = mapped_column(String(30))  # broker-side user ID

    # Encrypted bytea — never access directly, always use helpers below
    access_id_encrypted:       Mapped[bytes]            = mapped_column(LargeBinary, nullable=False)
    access_secret_encrypted:   Mapped[bytes]            = mapped_column(LargeBinary, nullable=False)
    session_token_encrypted:   Mapped[Optional[bytes]]  = mapped_column(LargeBinary)

    session_generated_at:      Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    session_expires_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active:                 Mapped[bool]             = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationship
    user: Mapped["User"] = relationship("User", back_populates="exchange_link")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<ExchangeLink provider={self.provider} account={self.account_ref}>"

    # ── Decrypt helpers ───────────────────────────────────

    def decrypt_access_id(self, db: Session) -> str:
        return self._decrypt(db, self.access_id_encrypted)

    def decrypt_access_secret(self, db: Session) -> str:
        return self._decrypt(db, self.access_secret_encrypted)

    def decrypt_session_token(self, db: Session) -> Optional[str]:
        if not self.session_token_encrypted:
            return None
        return self._decrypt(db, self.session_token_encrypted)

    # ── Encrypt helper ────────────────────────────────────

    @staticmethod
    def encrypt(db: Session, plain: str) -> bytes:
        row = db.execute(
            text("SELECT pgp_sym_encrypt(:v, :k) AS enc"),
            {"v": plain, "k": _ENCRYPTION_KEY},
        ).fetchone()
        return row.enc  # type: ignore[union-attr]

    @staticmethod
    def _decrypt(db: Session, cipher) -> str:
        from sqlalchemy import text

        if isinstance(cipher, memoryview):
            cipher = bytes(cipher)
        if isinstance(cipher, bytes):
            cipher = cipher.decode("utf-8", errors="replace")

        # Strip \x prefix if present
        cipher = cipher.lstrip("\\x").strip()

        row = db.execute(
            text("SELECT pgp_sym_decrypt(decode(:hex, 'hex'), :key) AS dec"),
            {"hex": cipher, "key": _ENCRYPTION_KEY},
        ).fetchone()
        return row.dec

    # ── Session state ─────────────────────────────────────

    @property
    def is_session_valid(self) -> bool:
        state = self.__dict__
        token = state.get("session_token_encrypted")
        expires = state.get("session_expires_at")

        if not token:
            return False
        if not expires:
            return False

        # Normalise to naive UTC for comparison
        now = datetime.utcnow()
        if hasattr(expires, "tzinfo") and expires.tzinfo is not None:
            # expires is timezone-aware — strip tzinfo for comparison
            expires = expires.replace(tzinfo=None)

        return now < expires

    @property
    def session_expires_ist(self) -> Optional[str]:
        if not self.session_expires_at:
            return None
        ist = self.session_expires_at + timedelta(hours=5, minutes=30)
        return ist.strftime("%Y-%m-%d %H:%M IST")


# ─────────────────────────────────────────────────────────────
# ExchangeLinkRepo
# ─────────────────────────────────────────────────────────────

class ExchangeLinkRepo:

    @staticmethod
    def get_for_user(db: Session, user_id: int) -> Optional[ExchangeLink]:
        return db.scalar(
            select(ExchangeLink).where(
                ExchangeLink.user_id  == user_id,
                ExchangeLink.is_active == True,
            )
        )

    @staticmethod
    def create(
        db: Session,
        user_id: int,
        access_id: str,
        access_secret: str,
        provider: str = "zerodha",
        account_ref: Optional[str] = None,
    ) -> ExchangeLink:
        """Create a new exchange link with encrypted values. Call db.commit() after."""
        link = ExchangeLink(
            user_id=user_id,
            provider=provider,
            account_ref=account_ref,
            access_id_encrypted=ExchangeLink.encrypt(db, access_id),
            access_secret_encrypted=ExchangeLink.encrypt(db, access_secret),
        )
        db.add(link)
        return link

    @staticmethod
    def refresh_session(
        db: Session,
        user_id: int,
        session_token: str,
    ) -> ExchangeLink:
        """
        Store a fresh session token after morning Kite login.
        Auto-sets expiry to next 6 AM IST.
        Call db.commit() after.
        """
        link = ExchangeLinkRepo.get_for_user(db, user_id)
        if not link:
            raise ValueError(f"No active exchange link for user_id={user_id}")
        link.session_token_encrypted = ExchangeLink.encrypt(db, session_token)
        link.session_generated_at    = datetime.now(timezone.utc)
        link.session_expires_at      = _next_6am_utc()
        return link

    @staticmethod
    def revoke_session(db: Session, user_id: int) -> None:
        """Clear the session token — called on logout or token expiry."""
        link = ExchangeLinkRepo.get_for_user(db, user_id)
        if link:
            link.session_token_encrypted = None
            link.session_generated_at    = None
            link.session_expires_at      = None

    @staticmethod
    def get_kite_client(db: Session, user_id: uuid.UUID):
        link = ExchangeLinkRepo.get_for_user(db, user_id)
        if not link:
            raise RuntimeError("No active exchange link found")
        if not link.is_session_valid:
            raise RuntimeError(
                f"Session token expired. Last expiry: {link.session_expires_ist}"
            )
        # Decrypt inside the same session that loaded the object
        api_key = link.decrypt_access_id(db)
        access_token = link.decrypt_session_token(db)

        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        return kite