from __future__ import annotations

import unittest
from datetime import datetime, timedelta
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

    def test_parse_time_suffix_is_not_part_of_title(self) -> None:
        for time_text in ("五點", "下午五點", "17:00", "三小時後", "明天五點"):
            for suffix in ("的時候", " 的時候 ", "時候", " 的 時候 "):
                with self.subTest(time_text=time_text, suffix=suffix):
                    prefix = f"提醒我在{time_text}"
                    expected = self.parser.parse(
                        f"{prefix}買兩份清燉牛肉湯", self.message, self.now
                    )
                    result = self.parser.parse(
                        f"{prefix}{suffix}買兩份清燉牛肉湯", self.message, self.now
                    )

                    self.assertEqual("買兩份清燉牛肉湯", result.title)
                    self.assertEqual(expected.remind_at, result.remind_at)
                    self.assertEqual((), result.missing_fields)
                    self.assertTrue(result.raw["consumed_time_text"].endswith(suffix.strip()))

    def test_time_suffix_is_consumed_across_parse_paths(self) -> None:
        for text in (
            "提醒我在2026-10-24 17:00的時候買湯",
            "提醒我在今晚的時候買湯",
            "提醒我在19分的時候買湯",
            "下午五點的時候提醒我買湯",
            "下午五點的時候買湯",
            "每天下午五點的時候提醒我買湯",
        ):
            with self.subTest(text=text):
                result = self.parser.parse(text, self.message, self.now)
                self.assertEqual("買湯", result.title)
                self.assertEqual((), result.missing_fields)

    def test_parse_preserves_time_suffix_inside_title(self) -> None:
        title = "買喝湯的時候要用的湯匙"
        result = self.parser.parse(f"提醒我在五點的時候{title}", self.message, self.now)

        self.assertEqual(title, result.title)

    def test_time_only_respects_explicit_month_day(self) -> None:
        now = datetime(2026, 10, 23, 12, 0, tzinfo=self.zone)
        for text in ("11/17 23:55", "11月17日 23:55", "十一月十七號 23:55"):
            for existing_date in (None, now + timedelta(days=1)):
                with self.subTest(text=text, existing_date=existing_date):
                    result = self.parser.parse_time_only(text, now, existing_date)
                    self.assertEqual(
                        datetime(2026, 11, 17, 23, 55, tzinfo=self.zone), result.remind_at
                    )
                    self.assertFalse(result.missing_time)
                    self.assertFalse(result.is_past)

    def test_time_only_month_day_uses_existing_year_rollover_rule(self) -> None:
        now = datetime(2026, 12, 23, 12, 0, tzinfo=self.zone)
        result = self.parser.parse_time_only("11/17 23:55", now)
        self.assertEqual(datetime(2027, 11, 17, 23, 55, tzinfo=self.zone), result.remind_at)

    def test_invalid_datetime_never_falls_back_to_today(self) -> None:
        for text in (
            "13/17 23:55",
            "11/31 23:55",
            "二月三十日 23:55",
            "2026-02-30 23:55",
            "25:00",
            "17:60",
            "99 分",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.parser.parse_time_only(text, self.now).remind_at)
                for source in (f"{text}提醒我買湯", f"提醒我在{text}買湯", f"{text}買湯"):
                    parsed = self.parser.parse(source, self.message, self.now)
                    self.assertIn("time", parsed.missing_fields)

    def test_time_only_preserves_existing_date_without_rolling_forward(self) -> None:
        for text, hour, minute in (("09:00", 9, 0), ("19 分", 8, 19)):
            existing = self.now.replace(hour=8)
            parsed = self.parser.parse_time_only(text, self.now, existing)
            self.assertEqual(existing.replace(hour=hour, minute=minute), parsed.remind_at)
            self.assertTrue(parsed.is_past)

    def test_past_date_only_keeps_missing_clock_flag(self) -> None:
        parsed = self.parser.parse_time_only("2026-06-02", self.now)
        self.assertTrue(parsed.is_past)
        self.assertTrue(parsed.missing_time)

    def test_time_only_and_creation_use_callers_timezone(self) -> None:
        zone = ZoneInfo("America/New_York")
        now = datetime(2026, 10, 23, 23, 0, tzinfo=zone)
        expected = datetime(2026, 10, 24, 9, 0, tzinfo=zone)
        self.assertEqual(expected, self.parser.parse_time_only("明早九點", now).remind_at)
        self.assertEqual(
            expected, self.parser.parse("提醒我明早九點買湯", self.message, now).remind_at
        )

    def test_search_can_read_clock_before_date(self) -> None:
        result = self.parser.parse_time_only("23:55 明天", self.now)
        self.assertEqual(self.now.replace(day=4, hour=23, minute=55), result.remind_at)

    def test_content_period_does_not_override_clock(self) -> None:
        parsed = self.parser.parse("五點買晚上要用的湯匙", self.message, self.now)
        self.assertEqual(5, parsed.remind_at.hour)
        self.assertEqual("買晚上要用的湯匙", parsed.title)

    def test_search_preserves_content_between_date_and_clock(self) -> None:
        for text, title, hour, minute in (
            ("明天買湯 23:55", "買湯", 23, 55),
            ("23:55 買湯 明天", "買湯", 23, 55),
            ("明天買晚餐 晚上九點", "買晚餐", 21, 0),
        ):
            with self.subTest(text=text):
                parsed = self.parser.parse(text, self.message, self.now)
                self.assertEqual(title, parsed.title)
                self.assertEqual(
                    (self.now + timedelta(days=1)).replace(hour=hour, minute=minute),
                    parsed.remind_at,
                )
                self.assertEqual((), parsed.missing_fields)

    def test_meal_names_do_not_supply_a_time(self) -> None:
        for title in ("買早餐", "買晚餐", "買早上的麵包", "買晚上的車票"):
            for prefix in ("提醒我", "每天提醒我"):
                with self.subTest(title=title, prefix=prefix):
                    parsed = self.parser.parse(prefix + title, self.message, self.now)
                    self.assertIn("time", parsed.missing_fields)
                    self.assertEqual(title, parsed.title)
                    self.assertIsNone(parsed.recurrence)
        parsed = self.parser.parse("提醒我明天早餐買湯", self.message, self.now)
        self.assertEqual("早餐買湯", parsed.title)
        self.assertIn("time", parsed.missing_fields)

    def test_week_prefixes_use_calendar_weeks(self) -> None:
        now = datetime(2026, 10, 23, 12, 0, tzinfo=self.zone)  # 週五
        for text, day, month, past in (
            ("週一九點", 26, 10, False),
            ("下週一九點", 26, 10, False),
            ("下下週一九點", 2, 11, False),
            ("本週一九點", 19, 10, True),
            ("這週一九點", 19, 10, True),
        ):
            with self.subTest(text=text):
                parsed = self.parser.parse_time_only(text, now)
                self.assertEqual(
                    datetime(2026, month, day, 9, 0, tzinfo=self.zone), parsed.remind_at
                )
                self.assertEqual(past, parsed.is_past)

    def test_recurrence_uses_shared_clock_and_period_rules(self) -> None:
        for time_text, hour in (("早上", 8), ("晚上九點", 21), ("17:00", 17)):
            for marker in ("每天", "每週一", "每月1號", "每年11/17"):
                with self.subTest(time_text=time_text, marker=marker):
                    parsed = self.parser.parse(
                        f"{marker}{time_text}提醒我買湯", self.message, self.now
                    )
                    self.assertIsNotNone(parsed.recurrence)
                    self.assertEqual(hour, parsed.recurrence.hour)
                    self.assertEqual("買湯", parsed.title)
        for time_text in ("25:00", "17:60"):
            with self.subTest(time_text=time_text):
                parsed = self.parser.parse(f"每天{time_text}提醒我買湯", self.message, self.now)
                self.assertIsNotNone(parsed.recurrence_error)

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

    def test_monthly_out_of_range_days_produce_recurrence_error(self) -> None:
        """『每個月 0 號』、『每個月 45 號』要單輪拒絕：不建 rule、不建 draft、
        parser 回傳 `recurrence_error` 讓 router 具體告知使用者。"""
        for text, expected_marker in [
            ("每個月 0 號 09:00 提醒我吃藥", "每個月 0 號"),
            ("每個月 45 號 09:00 提醒我吃藥", "每個月 45 號"),
        ]:
            r = self._parse(text)
            self.assertIsNone(r.recurrence, f"should not build rule for: {text!r}")
            self.assertIsNotNone(r.recurrence_error, f"should signal error: {text!r}")
            self.assertEqual(expected_marker, r.recurrence_error.marker_text)
            self.assertIn("1-31", r.recurrence_error.reason)
            # 錯誤情境下 title/remind_at 應為 None，讓 caller 明確判斷「不建 draft」
            self.assertIsNone(r.title)
            self.assertIsNone(r.remind_at)

    def test_monthly_partial_valid_days_are_kept(self) -> None:
        """『每個月 15, 45 號』只保留合法的 15，過濾掉 45（部分合法不算單輪拒絕）。"""
        r = self._parse("每個月 15, 45 號 09:00 提醒我發薪")
        self.assertIsNotNone(r.recurrence)
        self.assertEqual((15,), r.recurrence.month_days)
        self.assertIsNone(r.recurrence_error)

    def test_yearly_rejects_invalid_month(self) -> None:
        """『每年 13/25』月份超出 1-12；單輪拒絕。"""
        r = self._parse("每年 13/25 08:00 提醒我")
        self.assertIsNone(r.recurrence)
        self.assertIsNotNone(r.recurrence_error)
        self.assertEqual("每年 13/25", r.recurrence_error.marker_text)
        self.assertIn("1-12", r.recurrence_error.reason)

    def test_yearly_rejects_impossible_date(self) -> None:
        """『每年 2/30』日期不存在；單輪拒絕（reason 帶「日期無效」語意前綴 +
        該月最多天數，跟 monthly/interval 的錯誤訊息語氣一致）。"""
        r = self._parse("每年 2/30 08:00 提醒我")
        self.assertIsNone(r.recurrence)
        self.assertIsNotNone(r.recurrence_error)
        self.assertEqual("每年 2/30", r.recurrence_error.marker_text)
        self.assertEqual("日期無效，2 月最多 29 天", r.recurrence_error.reason)

    def test_yearly_rejects_april_31(self) -> None:
        r = self._parse("每年 4/31 08:00 提醒我")
        self.assertIsNotNone(r.recurrence_error)
        self.assertEqual("日期無效，4 月最多 30 天", r.recurrence_error.reason)

    def test_recurrence_title_preserves_yao_like_one_off_path(self) -> None:
        """一次性路徑刻意保留 `要`（例：`1號要去家樂福` → title 保留 要）。
        週期路徑應該一致，不再 strip leading 要。"""
        r = self._parse("每天 09:00 提醒我要運動")
        self.assertEqual("要運動", r.title)

    def test_yearly_allows_leap_day(self) -> None:
        """『每年 2/29』是合法規則（閏年才觸發）；`_is_valid_month_day` 用閏年當試探。"""
        r = self._parse("每年 2/29 08:00 提醒我生日")
        self.assertIsNotNone(r.recurrence)
        self.assertEqual(2, r.recurrence.year_month)
        self.assertEqual(29, r.recurrence.year_day)

    def test_interval_minutes(self) -> None:
        """每 15 分鐘 → interval_seconds=900、remind_at 為 now + 15min，
        title 從剩下的內容中提取（marker + '提醒我' 都被挖掉）。"""
        from remindly.reminders.models import RecurrencePeriod

        r = self._parse("每 15 分鐘提醒我喝水")
        self.assertIsNotNone(r.recurrence)
        self.assertEqual(RecurrencePeriod.INTERVAL, r.recurrence.period)
        self.assertEqual(900, r.recurrence.interval_seconds)
        self.assertIsNone(r.recurrence.hour)
        self.assertIsNone(r.recurrence.minute)
        self.assertEqual(self.now + timedelta(seconds=900), r.remind_at)
        self.assertEqual("喝水", r.title)

    def test_interval_units_all_convert(self) -> None:
        """分鐘/小時/天/日/週/周 都能轉成正確秒數。"""
        cases = [
            ("每 30 分鐘提醒我看螢幕", 30 * 60),
            ("每 2 小時提醒我看遠方", 2 * 3600),
            ("每 3 天提醒我澆花", 3 * 86400),
            ("每 5 日提醒我", 5 * 86400),  # 「日」為「天」別名
            ("每 2 週提醒我倒垃圾", 2 * 7 * 86400),
            ("每 4 周提醒我", 4 * 7 * 86400),  # 「周」為「週」別名
        ]
        for text, expected_seconds in cases:
            r = self._parse(text)
            self.assertIsNotNone(r.recurrence, f"failed for {text!r}")
            self.assertEqual(
                expected_seconds,
                r.recurrence.interval_seconds,
                f"seconds mismatch for {text!r}",
            )

    def test_interval_zero_rejected_with_positive_int_reason(self) -> None:
        """『每 0 分鐘』給不同的 reason（間隔需為正整數），不跟 <10min 混淆。"""
        r = self._parse("每 0 分鐘提醒我")
        self.assertIsNone(r.recurrence)
        self.assertIsNotNone(r.recurrence_error)
        self.assertEqual("每 0 分鐘", r.recurrence_error.marker_text)
        self.assertIn("正整數", r.recurrence_error.reason)

    def test_interval_overflow_rejected(self) -> None:
        """巨大的 n（例：99999999999 週）會讓 datetime + timedelta 超出可表示範圍，
        parser 要 catch OverflowError 轉成 RecurrenceError，不要 crash begin_create。"""
        r = self._parse("每 99999999999 週提醒我")
        self.assertIsNone(r.recurrence)
        self.assertIsNotNone(r.recurrence_error)
        self.assertIn("太大", r.recurrence_error.reason)

    def test_interval_below_minimum_rejected(self) -> None:
        """『每 1 分鐘』低於 10 分鐘下限，parser 回 RecurrenceError（單輪拒絕）。"""
        r = self._parse("每 1 分鐘提醒我要站起來")
        self.assertIsNone(r.recurrence)
        self.assertIsNotNone(r.recurrence_error)
        self.assertEqual("每 1 分鐘", r.recurrence_error.marker_text)
        self.assertIn("太頻繁", r.recurrence_error.reason)
        self.assertIn("10 分鐘", r.recurrence_error.reason)
        self.assertIsNone(r.title)
        self.assertIsNone(r.remind_at)

    def test_interval_at_boundary_accepted(self) -> None:
        """『每 10 分鐘』剛好等於下限，接受不拒絕。"""
        r = self._parse("每 10 分鐘提醒我喝水")
        self.assertIsNotNone(r.recurrence)
        self.assertEqual(600, r.recurrence.interval_seconds)
        self.assertIsNone(r.recurrence_error)

    def test_interval_without_title_still_produces_rule(self) -> None:
        """『每 10 分鐘提醒』沒事項 → rule 有效但 title 為空，走原本 draft 追問流程
        （不是單輪拒絕）。這對應 [[invalid-recurrence-single-turn-reject]] 的另一半：
        規則錯 → 單輪拒；規則對事項空 → 正常追問。"""
        r = self._parse("每 10 分鐘提醒")
        self.assertIsNotNone(r.recurrence)
        self.assertIsNone(r.recurrence_error)
        # title 應為 None 或空字串，讓 service.begin_create 走 DraftPrompt 追問
        self.assertFalse(r.title)


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
