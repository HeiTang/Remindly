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

    def test_parse_tonight_rolls_forward_when_default_time_has_passed(self) -> None:
        late_now = datetime(2026, 6, 3, 21, 0, tzinfo=self.zone)

        result = self.parser.parse("今晚提醒我洗衣服", self.message, late_now)

        self.assertEqual(datetime(2026, 6, 4, 20, 0, tzinfo=self.zone), result.remind_at)
        self.assertEqual("洗衣服", result.title)
        self.assertEqual((), result.missing_fields)

    def test_parse_weekday_without_clock_requires_follow_up(self) -> None:
        result = self.parser.parse("週五下午提醒我開會", self.message, self.now)

        self.assertEqual(datetime(2026, 6, 5, 15, 0, tzinfo=self.zone), result.remind_at)
        self.assertEqual("開會", result.title)
        self.assertEqual(("time",), result.missing_fields)

    def test_parse_same_weekday_can_target_today(self) -> None:
        friday_morning = datetime(2026, 6, 5, 9, 0, tzinfo=self.zone)

        result = self.parser.parse("週五下午三點提醒我開會", self.message, friday_morning)

        self.assertEqual(datetime(2026, 6, 5, 15, 0, tzinfo=self.zone), result.remind_at)
        self.assertEqual("開會", result.title)
        self.assertEqual((), result.missing_fields)

    def test_parse_chinese_number(self) -> None:
        self.assertEqual(9, parse_number("九"))
        self.assertEqual(10, parse_number("十"))
        self.assertEqual(12, parse_number("十二"))
        self.assertEqual(20, parse_number("二十"))
        self.assertEqual(23, parse_number("二十三"))


class ReminderParserCarrefourVariantsTest(unittest.TestCase):
    """九個表達同一件事的變體，全部應解析為 2026-07-30 21:00 的提醒，
    標題保留 08/01 / 1號 / 隔天等內容日期文字。"""

    EXPECTED_REMIND_AT_DATE = (2026, 7, 30, 21, 0)

    def setUp(self) -> None:
        self.zone = ZoneInfo("Asia/Taipei")
        self.now = datetime(2026, 7, 1, 12, 0, tzinfo=self.zone)
        self.parser = ReminderParser("Asia/Taipei")
        self.message = TelegramMessage(
            id=1,
            chat=TelegramChat(id=100, type="private"),
            from_user=TelegramUser(id=7, first_name="Orange", username="orange"),
            text="",
            entities=(),
        )

    def _assert_carrefour(self, text: str, expected_title_contains: str) -> None:
        result = self.parser.parse(text, self.message, self.now)
        expected = datetime(*self.EXPECTED_REMIND_AT_DATE, tzinfo=self.zone)
        title = result.title or ""
        self.assertEqual(expected, result.remind_at, f"time mismatch for: {text!r}")
        self.assertIn(
            expected_title_contains,
            title,
            f"title should contain {expected_title_contains!r} for: {text!r} → {title!r}",
        )
        self.assertNotIn("提醒", title, f"'提醒' leaked into title for: {text!r}")
        self.assertEqual(
            (),
            result.missing_fields,
            f"should be complete for: {text!r} → {result.missing_fields}",
        )

    def test_variant_1_zai_prefix_with_08_01(self) -> None:
        self._assert_carrefour(
            "在 7/30 21:00 提醒我 08/01 要去家樂福消費 799 以上",
            "08/01",
        )

    def test_variant_2_zai_prefix_with_1hao(self) -> None:
        self._assert_carrefour(
            "在 7/30 21:00 提醒我 1號要去家樂福消費 799 以上",
            "1號",
        )

    def test_variant_3_zai_prefix_with_next_day(self) -> None:
        self._assert_carrefour(
            "在 7/30 21:00 提醒我隔天要去家樂福消費 799 以上",
            "隔天",
        )

    def test_variant_4_no_zai_prefix(self) -> None:
        self._assert_carrefour(
            "7/30 21:00 提醒我 1號要去家樂福消費 799 以上",
            "1號",
        )

    def test_variant_5_time_after_reminder(self) -> None:
        self._assert_carrefour(
            "提醒我 7/30 21:00 1號要去家樂福消費 799 以上",
            "1號",
        )

    def test_variant_6_time_after_reminder_with_chinese_date(self) -> None:
        self._assert_carrefour(
            "提醒我 7/30 21:00 8月1號要去家樂福消費 799 以上",
            "8月1號",
        )

    def test_variant_7_zai_inside_content(self) -> None:
        self._assert_carrefour(
            "提醒我在7/30 21:00 8/1要去家樂福消費 799 以上",
            "8/1",
        )

    def test_variant_8_chinese_date_only(self) -> None:
        result = self.parser.parse(
            "提醒我7月30號 21:00 要去家樂福消費 799 以上", self.message, self.now
        )
        expected = datetime(*self.EXPECTED_REMIND_AT_DATE, tzinfo=self.zone)
        self.assertEqual(expected, result.remind_at)
        self.assertIn("家樂福", result.title or "")
        self.assertEqual((), result.missing_fields)

    def test_variant_9_multi_xia_weekday_needs_time(self) -> None:
        """今天 2026-07-01 週三，下下下下禮拜四 = 4 週後的週四 = 2026-07-30"""
        result = self.parser.parse("提醒我 下下下下禮拜四要去家樂福", self.message, self.now)
        expected_date = datetime(2026, 7, 30, 12, 0, tzinfo=self.zone)
        self.assertEqual(expected_date.date(), result.remind_at.date())
        self.assertIn("家樂福", result.title or "")
        self.assertEqual(("time",), result.missing_fields)


if __name__ == "__main__":
    unittest.main()
