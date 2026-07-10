from __future__ import annotations

import unittest
from pathlib import Path

from remindly import __version__
from remindly.app import format_startup_banner
from remindly.settings import Settings


class StartupBannerTest(unittest.TestCase):
    def _settings(self, **overrides) -> Settings:
        defaults = dict(
            token="secret",
            bot_username="ReminderBot",
            database_path=Path("/tmp/reminders.db"),
            default_timezone="Asia/Taipei",
            poll_timeout_seconds=25,
            scheduler_interval_seconds=10,
            draft_ttl_minutes=10,
            confirming_ttl_minutes=15,
            log_level="INFO",
        )
        defaults.update(overrides)
        return Settings(**defaults)

    def test_banner_contains_version_and_key_settings(self) -> None:
        lines = format_startup_banner(self._settings())
        joined = "\n".join(lines)

        self.assertIn(f"v{__version__}", joined)
        self.assertIn("@ReminderBot", joined)
        self.assertIn("Asia/Taipei", joined)
        self.assertIn("/tmp/reminders.db", joined)
        self.assertIn("poll_timeout_s      = 25", joined)
        self.assertIn("scheduler_interval  = 10s", joined)
        self.assertIn("draft_ttl_minutes   = 10", joined)
        self.assertIn("confirming_ttl_min  = 15", joined)
        self.assertIn("log_level           = INFO", joined)

    def test_banner_shows_placeholder_when_bot_username_missing(self) -> None:
        lines = format_startup_banner(self._settings(bot_username=None))
        joined = "\n".join(lines)

        self.assertIn("bot_username        = (未設定)", joined)
        self.assertNotIn("@None", joined)

    def test_banner_does_not_leak_token(self) -> None:
        lines = format_startup_banner(self._settings(token="super-secret-token"))
        joined = "\n".join(lines)

        self.assertNotIn("super-secret-token", joined)


if __name__ == "__main__":
    unittest.main()
