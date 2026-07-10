from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from remindly.reminders.models import RecurrencePeriod, RecurrenceRule
from remindly.reminders.recurrence import (
    deserialize_rule,
    format_rule,
    next_fire,
    serialize_rule,
)

ZONE = ZoneInfo("Asia/Taipei")


class SerializeRuleTest(unittest.TestCase):
    def test_daily_omits_unused_fields(self) -> None:
        rule = RecurrenceRule(period=RecurrencePeriod.DAILY, hour=9, minute=0)
        payload = serialize_rule(rule)
        self.assertNotIn("weekdays", payload)
        self.assertNotIn("month_days", payload)
        self.assertNotIn("year_month", payload)

        self.assertEqual(rule, deserialize_rule(payload))

    def test_weekly_roundtrip(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.WEEKLY,
            hour=9,
            minute=0,
            weekdays=(0, 2, 4),
        )
        self.assertEqual(rule, deserialize_rule(serialize_rule(rule)))

    def test_monthly_roundtrip(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=30,
            month_days=(1, 18, 25),
        )
        self.assertEqual(rule, deserialize_rule(serialize_rule(rule)))

    def test_yearly_roundtrip(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.YEARLY,
            hour=8,
            minute=0,
            year_month=12,
            year_day=25,
        )
        self.assertEqual(rule, deserialize_rule(serialize_rule(rule)))


class NextFireDailyTest(unittest.TestCase):
    def test_target_time_still_ahead_today(self) -> None:
        rule = RecurrenceRule(period=RecurrencePeriod.DAILY, hour=9, minute=0)
        after = datetime(2026, 7, 3, 8, 0, tzinfo=ZONE)
        self.assertEqual(datetime(2026, 7, 3, 9, 0, tzinfo=ZONE), next_fire(rule, after))

    def test_target_time_already_passed_rolls_to_tomorrow(self) -> None:
        rule = RecurrenceRule(period=RecurrencePeriod.DAILY, hour=9, minute=0)
        after = datetime(2026, 7, 3, 10, 0, tzinfo=ZONE)
        self.assertEqual(datetime(2026, 7, 4, 9, 0, tzinfo=ZONE), next_fire(rule, after))

    def test_target_time_at_exact_moment_rolls_forward(self) -> None:
        """避免 scheduler tick 剛好在觸發秒重排到同一時間造成無窮迴圈。"""
        rule = RecurrenceRule(period=RecurrencePeriod.DAILY, hour=9, minute=0)
        after = datetime(2026, 7, 3, 9, 0, tzinfo=ZONE)
        self.assertEqual(datetime(2026, 7, 4, 9, 0, tzinfo=ZONE), next_fire(rule, after))


class NextFireWeeklyTest(unittest.TestCase):
    def test_nearest_upcoming_weekday(self) -> None:
        """週三下午 4pm，設每週一/三/五 09:00 → 下次週五 09:00。"""
        rule = RecurrenceRule(
            period=RecurrencePeriod.WEEKLY,
            hour=9,
            minute=0,
            weekdays=(0, 2, 4),
        )
        after = datetime(2026, 7, 1, 16, 0, tzinfo=ZONE)  # Wed
        self.assertEqual(
            datetime(2026, 7, 3, 9, 0, tzinfo=ZONE),  # Fri
            next_fire(rule, after),
        )

    def test_today_matches_but_time_passed_finds_next_weekday(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.WEEKLY,
            hour=9,
            minute=0,
            weekdays=(2,),  # Wed only
        )
        after = datetime(2026, 7, 1, 10, 0, tzinfo=ZONE)  # Wed 10am
        self.assertEqual(
            datetime(2026, 7, 8, 9, 0, tzinfo=ZONE),  # Next Wed
            next_fire(rule, after),
        )

    def test_empty_weekdays_raises(self) -> None:
        rule = RecurrenceRule(period=RecurrencePeriod.WEEKLY, hour=9, minute=0)
        with self.assertRaises(ValueError):
            next_fire(rule, datetime(2026, 7, 1, 10, 0, tzinfo=ZONE))


class NextFireMonthlyTest(unittest.TestCase):
    def test_user_example_monthly_1_18_25(self) -> None:
        """使用者提出的例子：每月 1, 18, 25 號 09:00。"""
        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=0,
            month_days=(1, 18, 25),
        )
        after = datetime(2026, 7, 3, 10, 0, tzinfo=ZONE)
        self.assertEqual(datetime(2026, 7, 18, 9, 0, tzinfo=ZONE), next_fire(rule, after))

    def test_all_days_this_month_passed_rolls_to_next_month(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=0,
            month_days=(1, 18, 25),
        )
        after = datetime(2026, 7, 26, 10, 0, tzinfo=ZONE)
        self.assertEqual(datetime(2026, 8, 1, 9, 0, tzinfo=ZONE), next_fire(rule, after))

    def test_month_day_31_skipped_in_short_months(self) -> None:
        """規則指定 31 號，但 2 月沒有 31 號 → 下次跳過 2 月，去 3/31。"""
        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=0,
            month_days=(31,),
        )
        after = datetime(2026, 2, 1, 10, 0, tzinfo=ZONE)
        self.assertEqual(datetime(2026, 3, 31, 9, 0, tzinfo=ZONE), next_fire(rule, after))

    def test_cross_year_boundary(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=0,
            month_days=(15,),
        )
        after = datetime(2026, 12, 20, 10, 0, tzinfo=ZONE)
        self.assertEqual(datetime(2027, 1, 15, 9, 0, tzinfo=ZONE), next_fire(rule, after))


class NextFireYearlyTest(unittest.TestCase):
    def test_this_year_still_ahead(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.YEARLY,
            hour=8,
            minute=0,
            year_month=12,
            year_day=25,
        )
        after = datetime(2026, 7, 3, 10, 0, tzinfo=ZONE)
        self.assertEqual(datetime(2026, 12, 25, 8, 0, tzinfo=ZONE), next_fire(rule, after))

    def test_this_year_passed_rolls_to_next_year(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.YEARLY,
            hour=8,
            minute=0,
            year_month=1,
            year_day=1,
        )
        after = datetime(2026, 7, 3, 10, 0, tzinfo=ZONE)
        self.assertEqual(datetime(2027, 1, 1, 8, 0, tzinfo=ZONE), next_fire(rule, after))

    def test_leap_day_skips_non_leap_years(self) -> None:
        """2/29 在非閏年會跳過，繼續往下試。2026 非閏年 → 2028。"""
        rule = RecurrenceRule(
            period=RecurrencePeriod.YEARLY,
            hour=8,
            minute=0,
            year_month=2,
            year_day=29,
        )
        after = datetime(2026, 3, 1, 10, 0, tzinfo=ZONE)
        self.assertEqual(datetime(2028, 2, 29, 8, 0, tzinfo=ZONE), next_fire(rule, after))

    def test_leap_day_across_century_gap_2096_to_2104(self) -> None:
        """世紀邊界的閏年 gap：2096 leap → 2100 非閏 → 2104 leap（gap 8 年）。
        從 2097 起算，需要 9 年 lookahead 才能命中 2104/2/29。"""
        rule = RecurrenceRule(
            period=RecurrencePeriod.YEARLY,
            hour=8,
            minute=0,
            year_month=2,
            year_day=29,
        )
        after = datetime(2097, 3, 1, 10, 0, tzinfo=ZONE)
        self.assertEqual(datetime(2104, 2, 29, 8, 0, tzinfo=ZONE), next_fire(rule, after))


class FormatRuleTest(unittest.TestCase):
    def test_daily(self) -> None:
        rule = RecurrenceRule(period=RecurrencePeriod.DAILY, hour=9, minute=0)
        self.assertEqual("每天 09:00", format_rule(rule))

    def test_daily_pads_minute(self) -> None:
        rule = RecurrenceRule(period=RecurrencePeriod.DAILY, hour=8, minute=5)
        self.assertEqual("每天 08:05", format_rule(rule))

    def test_weekly_single(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.WEEKLY,
            hour=9,
            minute=0,
            weekdays=(0,),
        )
        self.assertEqual("每週一 09:00", format_rule(rule))

    def test_weekly_multi(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.WEEKLY,
            hour=9,
            minute=0,
            weekdays=(0, 2, 4),
        )
        self.assertEqual("每週一、三、五 09:00", format_rule(rule))

    def test_weekly_sunday_uses_日(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.WEEKLY,
            hour=9,
            minute=0,
            weekdays=(6,),
        )
        self.assertEqual("每週日 09:00", format_rule(rule))

    def test_monthly_single(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=0,
            month_days=(15,),
        )
        self.assertEqual("每月 15 號 09:00", format_rule(rule))

    def test_monthly_user_example(self) -> None:
        """使用者原例：每個月 1, 18, 25。"""
        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=0,
            month_days=(1, 18, 25),
        )
        self.assertEqual("每月 1, 18, 25 號 09:00", format_rule(rule))

    def test_yearly(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.YEARLY,
            hour=8,
            minute=0,
            year_month=12,
            year_day=25,
        )
        self.assertEqual("每年 12/25 08:00", format_rule(rule))

    def test_weekly_empty_weekdays_raises(self) -> None:
        """跟 next_fire 對齊：weekly 沒 weekdays 是 invalid rule。"""
        rule = RecurrenceRule(period=RecurrencePeriod.WEEKLY, hour=9, minute=0)
        with self.assertRaises(ValueError):
            format_rule(rule)

    def test_monthly_empty_days_raises(self) -> None:
        rule = RecurrencePeriod.MONTHLY
        with self.assertRaises(ValueError):
            format_rule(RecurrenceRule(period=rule, hour=9, minute=0))

    def test_yearly_missing_fields_raises(self) -> None:
        with self.assertRaises(ValueError):
            format_rule(
                RecurrenceRule(period=RecurrencePeriod.YEARLY, hour=8, minute=0)
            )

    def test_yearly_out_of_range_month_raises(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.YEARLY,
            hour=8,
            minute=0,
            year_month=13,
            year_day=25,
        )
        with self.assertRaises(ValueError):
            format_rule(rule)

    def test_yearly_impossible_date_raises(self) -> None:
        """4/31 不存在；format_rule 應與 parser 的 `is_valid_month_day` 對齊。"""
        rule = RecurrenceRule(
            period=RecurrencePeriod.YEARLY,
            hour=8,
            minute=0,
            year_month=4,
            year_day=31,
        )
        with self.assertRaises(ValueError):
            format_rule(rule)

    def test_hour_out_of_range_raises(self) -> None:
        """corrupted rule 帶 hour=99 應該 fail-fast，而非 render "99:99"。"""
        rule = RecurrenceRule(period=RecurrencePeriod.DAILY, hour=99, minute=0)
        with self.assertRaises(ValueError):
            format_rule(rule)

    def test_minute_out_of_range_raises(self) -> None:
        rule = RecurrenceRule(period=RecurrencePeriod.DAILY, hour=9, minute=60)
        with self.assertRaises(ValueError):
            format_rule(rule)

    def test_yearly_leap_day_is_valid(self) -> None:
        """2/29 合法（跟 parser 一致）；閏年才觸發。"""
        rule = RecurrenceRule(
            period=RecurrencePeriod.YEARLY,
            hour=8,
            minute=0,
            year_month=2,
            year_day=29,
        )
        self.assertEqual("每年 2/29 08:00", format_rule(rule))

    def test_weekly_out_of_range_weekday_raises(self) -> None:
        """越界 weekday（例如 9）不應該爆 IndexError；應該是明確的 ValueError。"""
        rule = RecurrenceRule(
            period=RecurrencePeriod.WEEKLY,
            hour=9,
            minute=0,
            weekdays=(9,),
        )
        with self.assertRaises(ValueError):
            format_rule(rule)

    def test_monthly_out_of_range_day_raises(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=0,
            month_days=(0,),
        )
        with self.assertRaises(ValueError):
            format_rule(rule)

    def test_format_sorts_weekdays_defensively(self) -> None:
        """若手動建 rule 沒排序（parser 有 sort，但 API 不強制），輸出仍要穩定。"""
        rule = RecurrenceRule(
            period=RecurrencePeriod.WEEKLY,
            hour=9,
            minute=0,
            weekdays=(4, 0, 2),
        )
        self.assertEqual("每週一、三、五 09:00", format_rule(rule))

    def test_format_sorts_month_days_defensively(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=0,
            month_days=(25, 1, 18),
        )
        self.assertEqual("每月 1, 18, 25 號 09:00", format_rule(rule))


class IntervalRecurrenceTest(unittest.TestCase):
    """Phase 5：INTERVAL period 覆蓋 serialize / next_fire / format_rule 三條路徑，
    確保 dataclass 擴充後與其他 period 隔離、彼此不互相污染。"""

    def test_interval_serialize_omits_hour_minute(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.INTERVAL, interval_seconds=900
        )
        payload = serialize_rule(rule)
        self.assertNotIn("hour", payload)
        self.assertNotIn("minute", payload)
        self.assertIn("interval_seconds", payload)
        self.assertEqual(rule, deserialize_rule(payload))

    def test_interval_next_fire_adds_seconds(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.INTERVAL, interval_seconds=600
        )
        after = datetime(2026, 7, 4, 8, 0, tzinfo=ZONE)
        from datetime import timedelta

        self.assertEqual(after + timedelta(seconds=600), next_fire(rule, after))

    def test_interval_next_fire_rejects_missing_seconds(self) -> None:
        rule = RecurrenceRule(period=RecurrencePeriod.INTERVAL)
        with self.assertRaises(ValueError):
            next_fire(rule, datetime(2026, 7, 4, 8, 0, tzinfo=ZONE))

    def test_format_rule_interval_picks_largest_unit(self) -> None:
        cases = [
            (600, "每 10 分鐘"),
            (900, "每 15 分鐘"),
            (3600, "每 1 小時"),
            (7200, "每 2 小時"),
            (86400, "每 1 天"),
            (172800, "每 2 天"),
            (604800, "每 1 週"),
            (1209600, "每 2 週"),
        ]
        for seconds, expected in cases:
            rule = RecurrenceRule(
                period=RecurrencePeriod.INTERVAL, interval_seconds=seconds
            )
            self.assertEqual(expected, format_rule(rule), f"failed for {seconds}s")

    def test_format_rule_interval_rejects_missing_seconds(self) -> None:
        rule = RecurrenceRule(period=RecurrencePeriod.INTERVAL)
        with self.assertRaises(ValueError):
            format_rule(rule)

    def test_non_interval_missing_hour_raises_in_format(self) -> None:
        """defensive：DAILY/WEEKLY/等在 hour/minute 為 None 時 format_rule 要 raise，
        避免出現「每天 None:None」。"""
        rule = RecurrenceRule(period=RecurrencePeriod.DAILY)
        with self.assertRaises(ValueError):
            format_rule(rule)


if __name__ == "__main__":
    unittest.main()
