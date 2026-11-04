from __future__ import annotations

import unittest

from remindly.reminders.text import (
    command_body,
    looks_like_reminder_request,
    new_reminder_text,
    strip_bot_mention,
)


class ReminderIntentTextTest(unittest.TestCase):
    def test_additional_request_operators_share_request_extraction_and_detection(self):
        for prefix in ("另外", "另", "再", "新增", "新建", "另開一筆", "請另外建立一個"):
            with self.subTest(prefix=prefix):
                text = prefix + "提醒我後天早上九點寄包裹"
                self.assertEqual("提醒我後天早上九點寄包裹", new_reminder_text(text))
                self.assertTrue(looks_like_reminder_request(text))

    def test_subject_mentions_updates_and_multiple_requests_are_not_extracted(self):
        for text in (
            "改成明天九點",
            "另外改成明天九點",
            "提醒我明天九點研究另外提醒功能",
            "明天九點提醒我寄信，另外提醒我後天拿包裹",
            "提醒我明天九點把「另外提醒我」寫進文件",
            "寄出另外一份報告",
        ):
            with self.subTest(text=text):
                self.assertIsNone(new_reminder_text(text))

    def test_weekday_particle_aliases_are_recognized_as_leading_time(self):
        for prefix in ("這個星期天", "這個禮拜日", "本週日", "下個星期一", "下下個禮拜天"):
            with self.subTest(prefix=prefix):
                self.assertTrue(looks_like_reminder_request(prefix + "九點提醒我買牛奶"))
                self.assertFalse(looks_like_reminder_request(prefix + "九點買牛奶"))


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
