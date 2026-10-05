import os
import unittest
from unittest.mock import patch

from market.auto_exit_config import AutoExitSettings


class AutoExitSettingsTests(unittest.TestCase):
    def test_database_values_override_environment_defaults(self):
        values = {
            "kite_auto_exit_enabled": False,
            "groww_auto_exit_enabled": True,
            "hard_stop_loss_pct": 9.5,
        }
        settings = AutoExitSettings(loader=lambda _db, _user, key, default: values.get(key, default))

        self.assertEqual(settings.refresh(object(), 1, monotonic_now=10, force=True), (False, True, 9.5))

    def test_values_hot_reload_after_refresh_interval(self):
        values = {"kite_auto_exit_enabled": True, "groww_auto_exit_enabled": True}
        settings = AutoExitSettings(
            refresh_seconds=2,
            loader=lambda _db, _user, key, default: values.get(key, default),
        )
        settings.refresh(object(), 1, monotonic_now=10, force=True)
        values["kite_auto_exit_enabled"] = False

        self.assertEqual(settings.refresh(object(), 1, monotonic_now=11), (True, True, 12.0))
        self.assertEqual(settings.refresh(object(), 1, monotonic_now=12), (False, True, 12.0))

    def test_environment_is_used_when_database_rows_are_missing(self):
        with patch.dict(os.environ, {
            "KITE_AUTO_EXIT_ENABLED": "false",
            "GROWW_AUTO_EXIT_ENABLED": "true",
        }):
            settings = AutoExitSettings(loader=lambda _db, _user, _key, default: default)
        self.assertEqual(settings.refresh(object(), 1, force=True), (False, True, 12.0))

    def test_invalid_stop_percentage_falls_back_to_twelve(self):
        values = {"hard_stop_loss_pct": 0}
        settings = AutoExitSettings(loader=lambda _db, _user, key, default: values.get(key, default))
        settings.refresh(object(), 1, force=True)
        self.assertEqual(settings.hard_stop_loss_pct, 12.0)


if __name__ == "__main__":
    unittest.main()
