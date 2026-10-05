"""Hot-reloaded automatic-exit settings shared by broker monitors."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable


TRUE_VALUES = {"1", "true", "yes", "on"}
DEFAULT_HARD_STOP_LOSS_PCT = 12.0


def _env_bool(name: str, default: bool = True) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() in TRUE_VALUES


class AutoExitSettings:
    """Read exit switches from market_configs without querying every tick."""

    def __init__(
        self,
        refresh_seconds: float = 2.0,
        loader: Callable | None = None,
    ):
        self.refresh_seconds = refresh_seconds
        self.kite_enabled = _env_bool("KITE_AUTO_EXIT_ENABLED")
        self.groww_enabled = _env_bool("GROWW_AUTO_EXIT_ENABLED")
        self.hard_stop_loss_pct = self._valid_stop_pct(
            os.getenv("HARD_STOP_LOSS_PCT", DEFAULT_HARD_STOP_LOSS_PCT)
        )
        self._last_refresh = 0.0
        self._lock = threading.Lock()
        self._loader = loader

    def refresh(
        self,
        db,
        user_id: int,
        monotonic_now: float | None = None,
        force: bool = False,
    ) -> tuple[bool, bool, float]:
        now = time.monotonic() if monotonic_now is None else monotonic_now
        with self._lock:
            if not force and now - self._last_refresh < self.refresh_seconds:
                return self.kite_enabled, self.groww_enabled, self.hard_stop_loss_pct

            loader = self._loader
            if loader is None:
                from trading.repositories import MarketConfigRepo
                loader = MarketConfigRepo.get

            self.kite_enabled = bool(loader(
                db, user_id, "kite_auto_exit_enabled", self.kite_enabled
            ))
            self.groww_enabled = bool(loader(
                db, user_id, "groww_auto_exit_enabled", self.groww_enabled
            ))
            self.hard_stop_loss_pct = self._valid_stop_pct(loader(
                db, user_id, "hard_stop_loss_pct", self.hard_stop_loss_pct
            ))
            self._last_refresh = now
            return self.kite_enabled, self.groww_enabled, self.hard_stop_loss_pct

    @staticmethod
    def _valid_stop_pct(value) -> float:
        try:
            percentage = float(value)
        except (TypeError, ValueError):
            return DEFAULT_HARD_STOP_LOSS_PCT
        return percentage if 0.1 <= percentage <= 99.0 else DEFAULT_HARD_STOP_LOSS_PCT
