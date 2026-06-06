from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from remindly.reminders.parser import ReminderParser, parse_number
from remindly.telegram.models import TelegramChat, TelegramMessage, TelegramUser


class ReminderParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.zone = ZoneInfo("Asia/Taipei")
        self.now = datetime(2026, 6, 3, 12, 0, tzinfo=self.zone)
        self.parser = ReminderParser("Asia/Taipei")
        self.message = TelegramMessage(
            id=1,
            chat=TelegramChat(id=100, type="private"),
            from_user=TelegramUser(id=7, first_name="Orange", username="orange"),
            text="",
            entities=(),
        )

    def test_parse_tomorrow_afternoon(self) -> None:
        result = self.parser.parse("明天下午三點提醒我倒垃圾", self.message, self.now)

        self.assertEqual(datetime(2026, 6, 4, 15, 0, tzinfo=self.zone), result.remind_at)
        self.assertEqual("倒垃圾", result.title)
        self.assertEqual((), result.missing_fields)
        self.assertEqual("@orange", result.participants[0].display_name)

    def test_parse_relative_hours(self) -> None:
        result = self.parser.parse("3 小時後提醒我喝水", self.message, self.now)

        self.assertEqual(datetime(2026, 6, 3, 15, 0, tzinfo=self.zone), result.remind_at)
        self.assertEqual("喝水", result.title)
        self.assertEqual((), result.missing_fields)

    def test_parse_date_without_time_requires_follow_up(self) -> None:
        result = self.parser.parse("明天提醒我倒垃圾", self.message, self.now)

        self.assertEqual(datetime(2026, 6, 4, 12, 0, tzinfo=self.zone), result.remind_at)
        self.assertEqual("倒垃圾", result.title)
        self.assertEqual(("time",), result.missing_fields)

    def test_parse_absolute_datetime(self) -> None:
        result = self.parser.parse("2026-06-04 15:00 倒垃圾", self.message, self.now)

        self.assertEqual(datetime(2026, 6, 4, 15, 0, tzinfo=self.zone), result.remind_at)
        self.assertEqual("倒垃圾", result.title)
        self.assertEqual((), result.missing_fields)

    def test_parse_multi_participants(self) -> None:
        result = self.parser.parse("明天下午三點提醒我和 @alice 倒垃圾", self.message, self.now)

        self.assertEqual("倒垃圾", result.title)
        self.assertEqual(["@orange", "@alice"], [p.display_name for p in result.participants])

    def test_parse_tonight(self) -> None:
        result = self.parser.parse("今晚提醒我洗衣服", self.message, self.now)

        self.assertEqual(datetime(2026, 6, 3, 20, 0, tzinfo=self.zone), result.remind_at)
        self.assertEqual("洗衣服", result.title)
        self.assertEqual((), result.missing_fields)

    def test_parse_tomorrow_morning(self) -> None:
        result = self.parser.parse("明早提醒我買早餐", self.message, self.now)

        self.assertEqual(datetime(2026, 6, 4, 8, 0, tzinfo=self.zone), result.remind_at)
        self.assertEqual("買早餐", result.title)
        self.assertEqual((), result.missing_fields)

    def test_parse_half_hour_later(self) -> None:
        result = self.parser.parse("半小時後提醒我喝水", self.message, self.now)

        self.assertEqual(datetime(2026, 6, 3, 12, 30, tzinfo=self.zone), result.remind_at)
        self.assertEqual("喝水", result.title)
        self.assertEqual((), result.missing_fields)

    def test_parse_weekday_without_clock_requires_follow_up(self) -> None:
        result = self.parser.parse("週五下午提醒我開會", self.message, self.now)

        self.assertEqual(datetime(2026, 6, 5, 12, 0, tzinfo=self.zone), result.remind_at)
        self.assertEqual("開會", result.title)
        self.assertEqual(("time",), result.missing_fields)

    def test_parse_chinese_number(self) -> None:
        self.assertEqual(9, parse_number("九"))
        self.assertEqual(10, parse_number("十"))
        self.assertEqual(12, parse_number("十二"))
        self.assertEqual(20, parse_number("二十"))
        self.assertEqual(23, parse_number("二十三"))


if __name__ == "__main__":
    unittest.main()
