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
            "max_loss_amount": 4000,
            "ignore_symbols": " nifty26oct25000ce, RELIANCE ",
        }
        settings = AutoExitSettings(loader=lambda _db, _user, key, default: values.get(key, default))

        self.assertEqual(settings.refresh(object(), 1, monotonic_now=10, force=True), (False, True, 9.5))
        self.assertEqual(settings.max_loss_amount, 4000.0)
        self.assertEqual(settings.ignored_symbols, {"NIFTY26OCT25000CE", "RELIANCE"})

    def test_values_hot_reload_after_refresh_interval(self):
        values = {"kite_auto_exit_enabled": True, "groww_auto_exit_enabled": True}
        settings = AutoExitSettings(
            refresh_seconds=2,
            loader=lambda _db, _user, key, default: values.get(key, default),
        )
        settings.refresh(object(), 1, monotonic_now=10, force=True)
        values["kite_auto_exit_enabled"] = False

        self.assertEqual(settings.refresh(object(), 1, monotonic_now=11), (True, True, 10.0))
        self.assertEqual(settings.refresh(object(), 1, monotonic_now=12), (False, True, 10.0))

    def test_environment_is_used_when_database_rows_are_missing(self):
        with patch.dict(os.environ, {
            "KITE_AUTO_EXIT_ENABLED": "false",
            "GROWW_AUTO_EXIT_ENABLED": "true",
        }):
            settings = AutoExitSettings(loader=lambda _db, _user, _key, default: default)
        self.assertEqual(settings.refresh(object(), 1, force=True), (False, True, 10.0))

    def test_invalid_stop_percentage_falls_back_to_ten(self):
        values = {"hard_stop_loss_pct": 0}
        settings = AutoExitSettings(loader=lambda _db, _user, key, default: values.get(key, default))
        settings.refresh(object(), 1, force=True)
        self.assertEqual(settings.hard_stop_loss_pct, 10.0)

    def test_blank_max_loss_amount_disables_rupee_cap(self):
        values = {"max_loss_amount": ""}
        settings = AutoExitSettings(loader=lambda _db, _user, key, default: values.get(key, default))
        settings.refresh(object(), 1, force=True)
        self.assertIsNone(settings.max_loss_amount)

    def test_ignored_symbols_hot_reload_and_accept_newlines(self):
        values = {"ignore_symbols": "NIFTY26OCT25000CE"}
        settings = AutoExitSettings(
            refresh_seconds=2,
            loader=lambda _db, _user, key, default: values.get(key, default),
        )
        settings.refresh(object(), 1, monotonic_now=10, force=True)
        values["ignore_symbols"] = "reliance\nBANKNIFTY26OCT55000PE"

        settings.refresh(object(), 1, monotonic_now=11)
        self.assertEqual(settings.ignored_symbols, {"NIFTY26OCT25000CE"})
        settings.refresh(object(), 1, monotonic_now=12)
        self.assertEqual(
            settings.ignored_symbols,
            {"RELIANCE", "BANKNIFTY26OCT55000PE"},
        )


if __name__ == "__main__":
    unittest.main()
