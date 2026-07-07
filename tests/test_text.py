from __future__ import annotations

import unittest

from remindly.reminders.text import command_body, strip_bot_mention


class CommandBodyTest(unittest.TestCase):
    """回歸：`command_body(text, cmd, bot_username=None)` 之前會在 line 49
    無條件呼叫 `re.escape(bot_username)`，拋 `TypeError`。使用者部署未設
    `BOT_USERNAME` 時所有 slash command 都會被 app 層默默 catch，看不到回覆。"""

    def test_matches_bare_command_when_bot_username_none(self) -> None:
        self.assertEqual("hello", command_body("/remind hello", "remind", None))

    def test_matches_at_suffix_command_when_bot_username_none(self) -> None:
        """未設 BOT_USERNAME 時仍應接受 `/cmd@AnyBot` 尾綴。"""
        self.assertEqual(
            "hello",
            command_body("/remind@SomeBot hello", "remind", None),
        )

    def test_does_not_match_wrong_command_when_bot_username_none(self) -> None:
        self.assertIsNone(command_body("/other hello", "remind", None))

    def test_matches_at_suffix_command_when_bot_username_set(self) -> None:
        self.assertEqual(
            "hello",
            command_body("/remind@ReminderBot hello", "remind", "ReminderBot"),
        )

    def test_bare_command_still_matches_when_bot_username_set(self) -> None:
        self.assertEqual(
            "hello",
            command_body("/remind hello", "remind", "ReminderBot"),
        )


class StripBotMentionTest(unittest.TestCase):
    def test_strips_leading_mention(self) -> None:
        self.assertEqual(
            "提醒我倒垃圾",
            strip_bot_mention("@ReminderBot 提醒我倒垃圾", "ReminderBot"),
        )

    def test_no_change_when_no_mention(self) -> None:
        self.assertEqual(
            "提醒我倒垃圾",
            strip_bot_mention("提醒我倒垃圾", "ReminderBot"),
        )

    def test_no_change_when_bot_username_missing(self) -> None:
        self.assertEqual("hello", strip_bot_mention("hello", None))


if __name__ == "__main__":
    unittest.main()
