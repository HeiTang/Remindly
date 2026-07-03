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


class ReminderParserRecurrenceTest(unittest.TestCase):
    """Phase 2：自然語言 → RecurrenceRule。"""

    def setUp(self) -> None:
        from remindly.reminders.parser import ReminderParser

        self.zone = ZoneInfo("Asia/Taipei")
        # 2026-07-04（週六）08:00
        self.now = datetime(2026, 7, 4, 8, 0, tzinfo=self.zone)
        self.parser = ReminderParser("Asia/Taipei")
        self.message = TelegramMessage(
            id=1,
            chat=TelegramChat(id=100, type="private"),
            from_user=TelegramUser(id=7, first_name="Orange", username="orange"),
            text="",
            entities=(),
        )

    def _parse(self, text: str):
        return self.parser.parse(text, self.message, self.now)

    def test_daily_09_00(self) -> None:
        from remindly.reminders.models import RecurrencePeriod

        r = self._parse("每天 09:00 提醒我吃藥")
        self.assertIsNotNone(r.recurrence)
        self.assertEqual(RecurrencePeriod.DAILY, r.recurrence.period)
        self.assertEqual(9, r.recurrence.hour)
        self.assertEqual(0, r.recurrence.minute)
        self.assertIn("吃藥", r.title or "")
        self.assertEqual((), r.missing_fields)
        # 今天 8am → 首次觸發 = 今天 9am
        self.assertEqual(datetime(2026, 7, 4, 9, 0, tzinfo=self.zone), r.remind_at)

    def test_daily_with_period_word(self) -> None:
        r = self._parse("每天早上 9 點提醒我運動")
        self.assertIsNotNone(r.recurrence)
        self.assertEqual(9, r.recurrence.hour)
        self.assertIn("運動", r.title or "")

    def test_weekly_single_day(self) -> None:
        from remindly.reminders.models import RecurrencePeriod

        r = self._parse("每週一 09:00 提醒我開會")
        self.assertEqual(RecurrencePeriod.WEEKLY, r.recurrence.period)
        self.assertEqual((0,), r.recurrence.weekdays)
        self.assertIn("開會", r.title or "")

    def test_weekly_multi_days_no_separator(self) -> None:
        r = self._parse("每週一三五 09:00 提醒我運動")
        self.assertEqual((0, 2, 4), r.recurrence.weekdays)

    def test_weekly_multi_days_with_dun_separator(self) -> None:
        r = self._parse("每週一、三、五 09:00 提醒我運動")
        self.assertEqual((0, 2, 4), r.recurrence.weekdays)

    def test_weekly_synonyms(self) -> None:
        """禮拜、星期 也算 weekly marker。"""
        for text in ["每禮拜二 09:00 提醒我開會", "每星期二 09:00 提醒我開會"]:
            r = self._parse(text)
            self.assertIsNotNone(r.recurrence)
            self.assertEqual((1,), r.recurrence.weekdays)

    def test_monthly_single_day(self) -> None:
        from remindly.reminders.models import RecurrencePeriod

        r = self._parse("每個月 1 號 09:00 提醒我付房租")
        self.assertEqual(RecurrencePeriod.MONTHLY, r.recurrence.period)
        self.assertEqual((1,), r.recurrence.month_days)
        self.assertIn("付房租", r.title or "")

    def test_monthly_multi_days_user_example(self) -> None:
        """使用者的原始請求：每個月 1, 18, 25 提醒我繳信用卡"""
        r = self._parse("每個月 1, 18, 25 號 09:00 提醒我繳信用卡")
        self.assertEqual((1, 18, 25), r.recurrence.month_days)
        self.assertIn("繳信用卡", r.title or "")
        # 今天 7/4 → 下一次 = 7/18
        self.assertEqual(datetime(2026, 7, 18, 9, 0, tzinfo=self.zone), r.remind_at)

    def test_monthly_multi_days_with_dun_separator(self) -> None:
        r = self._parse("每個月 1、18、25 號 09:00 提醒我繳信用卡")
        self.assertEqual((1, 18, 25), r.recurrence.month_days)

    def test_monthly_short_form_without_ge(self) -> None:
        """『每月』（不加『個』）也算 monthly marker。"""
        r = self._parse("每月 15 號 09:00 提醒我發薪")
        self.assertEqual((15,), r.recurrence.month_days)

    def test_yearly_chinese_date(self) -> None:
        from remindly.reminders.models import RecurrencePeriod

        r = self._parse("每年 12月25號 08:00 提醒我聖誕節")
        self.assertEqual(RecurrencePeriod.YEARLY, r.recurrence.period)
        self.assertEqual(12, r.recurrence.year_month)
        self.assertEqual(25, r.recurrence.year_day)
        self.assertIn("聖誕節", r.title or "")

    def test_yearly_slash_date(self) -> None:
        r = self._parse("每年 12/25 08:00 提醒我聖誕節")
        self.assertEqual(12, r.recurrence.year_month)
        self.assertEqual(25, r.recurrence.year_day)

    def test_non_recurring_still_works(self) -> None:
        """一次性提醒不應被誤認為週期。"""
        r = self._parse("明天下午三點提醒我倒垃圾")
        self.assertIsNone(r.recurrence)
        self.assertEqual(datetime(2026, 7, 5, 15, 0, tzinfo=self.zone), r.remind_at)

    def test_monthly_rejects_zero_and_out_of_range_days(self) -> None:
        """『每個月 0 號』、『每個月 45 號』會讓 next_fire 崩掉；應該 fall through。"""
        for text in [
            "每個月 0 號 09:00 提醒我吃藥",
            "每個月 45 號 09:00 提醒我吃藥",
        ]:
            r = self._parse(text)
            self.assertIsNone(r.recurrence, f"should not build rule for: {text!r}")

    def test_monthly_partial_valid_days_are_kept(self) -> None:
        """『每個月 15, 45 號』只保留合法的 15，過濾掉 45。"""
        r = self._parse("每個月 15, 45 號 09:00 提醒我發薪")
        self.assertIsNotNone(r.recurrence)
        self.assertEqual((15,), r.recurrence.month_days)

    def test_yearly_rejects_invalid_month(self) -> None:
        """『每年 13/25』月份超出 1-12；不建立規則。"""
        r = self._parse("每年 13/25 08:00 提醒我")
        self.assertIsNone(r.recurrence)

    def test_yearly_rejects_impossible_date(self) -> None:
        """『每年 2/30』日期不存在；不建立規則（會讓 next_fire 崩掉）。"""
        r = self._parse("每年 2/30 08:00 提醒我")
        self.assertIsNone(r.recurrence)

    def test_yearly_allows_leap_day(self) -> None:
        """『每年 2/29』是合法規則（閏年才觸發）；`_is_valid_month_day` 用閏年當試探。"""
        r = self._parse("每年 2/29 08:00 提醒我生日")
        self.assertIsNotNone(r.recurrence)
        self.assertEqual(2, r.recurrence.year_month)
        self.assertEqual(29, r.recurrence.year_day)


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

    def test_partial_minute_uses_current_hour(self) -> None:
        """『19 分要起立』在 08:02 → 08:19。"""
        result = self.parser.parse(
            "提醒我 19 分要起立",
            self.message,
            datetime(2026, 7, 3, 8, 2, tzinfo=self.zone),
        )
        self.assertEqual(
            datetime(2026, 7, 3, 8, 19, tzinfo=self.zone),
            result.remind_at,
        )
        self.assertIn("起立", result.title or "")
        self.assertEqual((), result.missing_fields)

    def test_partial_minute_rolls_to_next_hour_when_past(self) -> None:
        """『19 分』在 08:25 → 09:19（分鐘已過就滾到下一小時）。"""
        result = self.parser.parse(
            "提醒我 19 分要起立",
            self.message,
            datetime(2026, 7, 3, 8, 25, tzinfo=self.zone),
        )
        self.assertEqual(
            datetime(2026, 7, 3, 9, 19, tzinfo=self.zone),
            result.remind_at,
        )

    def test_partial_minute_does_not_match_full_minute_unit(self) -> None:
        """『19 分鐘後』應走 RELATIVE 而非 PARTIAL_MINUTE（09:02 - 19 min 應為 09:21）。"""
        result = self.parser.parse(
            "提醒我 19 分鐘後要起立",
            self.message,
            datetime(2026, 7, 3, 8, 2, tzinfo=self.zone),
        )
        self.assertEqual(
            datetime(2026, 7, 3, 8, 21, tzinfo=self.zone),
            result.remind_at,
        )

    def test_relative_weeks(self) -> None:
        result = self.parser.parse(
            "提醒我 3 週後要起立",
            self.message,
            datetime(2026, 7, 3, 8, 2, tzinfo=self.zone),
        )
        self.assertEqual(
            datetime(2026, 7, 24, 8, 2, tzinfo=self.zone),
            result.remind_at,
        )

    def test_relative_star_qi_full_form(self) -> None:
        result = self.parser.parse(
            "提醒我 3 個星期後要起立",
            self.message,
            datetime(2026, 7, 3, 8, 2, tzinfo=self.zone),
        )
        self.assertEqual(
            datetime(2026, 7, 24, 8, 2, tzinfo=self.zone),
            result.remind_at,
        )

    def test_relative_months_uses_calendar_arithmetic(self) -> None:
        """3 個月後：走 calendar 加減，不是 30 天近似。"""
        result = self.parser.parse(
            "提醒我 3 個月後要起立",
            self.message,
            datetime(2026, 7, 3, 8, 2, tzinfo=self.zone),
        )
        self.assertEqual(
            datetime(2026, 10, 3, 8, 2, tzinfo=self.zone),
            result.remind_at,
        )

    def test_relative_months_clamps_end_of_month(self) -> None:
        """1/31 + 1 個月 → 2/28（clamp 到當月最後一天）。"""
        result = self.parser.parse(
            "提醒我 1 個月後要起立",
            self.message,
            datetime(2026, 1, 31, 8, 0, tzinfo=self.zone),
        )
        self.assertEqual(
            datetime(2026, 2, 28, 8, 0, tzinfo=self.zone),
            result.remind_at,
        )

    def test_variant_9_multi_xia_weekday_needs_time(self) -> None:
        """今天 2026-07-01 週三，下下下下禮拜四 = 4 週後的週四 = 2026-07-30"""
        result = self.parser.parse("提醒我 下下下下禮拜四要去家樂福", self.message, self.now)
        expected_date = datetime(2026, 7, 30, 12, 0, tzinfo=self.zone)
        self.assertEqual(expected_date.date(), result.remind_at.date())
        self.assertIn("家樂福", result.title or "")
        self.assertEqual(("time",), result.missing_fields)


if __name__ == "__main__":
    unittest.main()
