from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from remindly.reminders.models import (
    MentionKind,
    Participant,
    RecurrencePeriod,
    RecurrenceRule,
    Reminder,
    ReminderDraft,
    ReminderStatus,
)
from remindly.reminders.renderer import ReminderRenderer
from remindly.reminders.service import ReminderListFilter, ReminderListGroup

ZONE = ZoneInfo("Asia/Taipei")


def _reminder(**overrides) -> Reminder:
    defaults = dict(
        id="rmd_1",
        short_id="R-ABC",
        chat_id=100,
        chat_type="private",
        creator_user_id=7,
        title="繳信用卡",
        remind_at=datetime(2026, 7, 18, 9, 0, tzinfo=ZONE),
        timezone="Asia/Taipei",
        status=ReminderStatus.PENDING,
        source_text="",
        parse_result={},
        created_at=datetime(2026, 7, 4, 8, 0, tzinfo=ZONE),
        updated_at=datetime(2026, 7, 4, 8, 0, tzinfo=ZONE),
        recurrence=None,
    )
    defaults.update(overrides)
    return Reminder(**defaults)


def _draft(**overrides) -> ReminderDraft:
    defaults = dict(
        id="draft_1",
        chat_id=100,
        chat_type="private",
        creator_user_id=7,
        timezone="Asia/Taipei",
        source_text="",
        title="繳信用卡",
        remind_at=datetime(2026, 7, 18, 9, 0, tzinfo=ZONE),
        participants=[
            Participant(
                user_id=7,
                username="orange",
                display_name="@orange",
                mention_kind=MentionKind.USERNAME,
            )
        ],
        missing_fields=[],
        parse_result={},
        recurrence=None,
    )
    defaults.update(overrides)
    return ReminderDraft(**defaults)


class RenderConfirmationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = ReminderRenderer()

    def test_one_off_shows_time_label(self) -> None:
        draft = _draft()
        text = self.renderer.render_confirmation(draft)
        self.assertIn("時間：", text)
        self.assertNotIn("重複：", text)
        self.assertNotIn("下次：", text)

    def test_recurring_shows_repeat_and_next(self) -> None:
        draft = _draft(
            recurrence=RecurrenceRule(
                period=RecurrencePeriod.MONTHLY,
                hour=9,
                minute=0,
                month_days=(1, 18, 25),
            )
        )
        text = self.renderer.render_confirmation(draft)
        self.assertIn("重複：每月 1, 18, 25 號 09:00", text)
        self.assertIn("下次：2026-07-18 09:00", text)
        self.assertNotIn("時間：", text)


class RenderCreatedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = ReminderRenderer()

    def test_one_off_shows_time_label(self) -> None:
        text = self.renderer.render_created(_reminder())
        self.assertIn("時間：", text)
        self.assertNotIn("重複：", text)

    def test_recurring_shows_repeat_and_next(self) -> None:
        text = self.renderer.render_created(
            _reminder(
                recurrence=RecurrenceRule(
                    period=RecurrencePeriod.DAILY, hour=9, minute=0
                )
            )
        )
        self.assertIn("重複：每天 09:00", text)
        self.assertIn("下次：", text)
        self.assertIn("取消：/cancel R-ABC", text)


class RenderDetailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = ReminderRenderer()

    def test_recurring_shows_repeat_and_next(self) -> None:
        text = self.renderer.render_detail(
            _reminder(
                recurrence=RecurrenceRule(
                    period=RecurrencePeriod.WEEKLY,
                    hour=9,
                    minute=0,
                    weekdays=(0, 2, 4),
                )
            ),
            [],
        )
        self.assertIn("重複：每週一、三、五 09:00", text)
        self.assertIn("下次：", text)


class RenderDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = ReminderRenderer()

    def test_one_off_omits_repeat_line(self) -> None:
        text = self.renderer.render_delivery(_reminder(), [])
        self.assertNotIn("重複：", text)

    def test_recurring_shows_repeat_line(self) -> None:
        text = self.renderer.render_delivery(
            _reminder(
                recurrence=RecurrenceRule(
                    period=RecurrencePeriod.MONTHLY,
                    hour=9,
                    minute=0,
                    month_days=(1, 18, 25),
                )
            ),
            [],
        )
        self.assertIn("重複：每月 1, 18, 25 號 09:00", text)


class RenderSkippedNextTest(unittest.TestCase):
    """Phase 4a: 使用者按「跳過下次」後看到的訊息。"""

    def setUp(self) -> None:
        self.renderer = ReminderRenderer()

    def test_shows_short_id_and_next_time(self) -> None:
        text = self.renderer.render_skipped_next(_reminder())
        self.assertIn("已跳過下次", text)
        self.assertIn("R-ABC", text)
        self.assertIn("下次：2026-07-18 09:00", text)
        self.assertIn("繳信用卡", text)

    def test_escapes_html_special_chars_in_title(self) -> None:
        text = self.renderer.render_skipped_next(_reminder(title="<script>"))
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)


class RenderSeriesCancelledTest(unittest.TestCase):
    """Phase 4a: 使用者按「取消整個系列」後看到的訊息。"""

    def setUp(self) -> None:
        self.renderer = ReminderRenderer()

    def test_shows_short_id_and_title(self) -> None:
        text = self.renderer.render_series_cancelled(_reminder())
        self.assertIn("已取消整個系列", text)
        self.assertIn("R-ABC", text)
        self.assertIn("繳信用卡", text)
        # 取消後不再顯示下次時間
        self.assertNotIn("下次", text)

    def test_escapes_html_special_chars_in_title(self) -> None:
        text = self.renderer.render_series_cancelled(_reminder(title="<b>x</b>"))
        self.assertIn("&lt;b&gt;", text)
        self.assertNotIn("<b>x</b>", text)


class RenderGroupedListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = ReminderRenderer()

    def test_recurring_reminder_shows_bracket_prefix(self) -> None:
        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=0,
            month_days=(1, 18, 25),
        )
        groups = [
            ReminderListGroup(
                creator_user_id=7,
                creator_label="你",
                reminders=[
                    _reminder(short_id="R-REC", recurrence=rule),
                    _reminder(id="rmd_2", short_id="R-ONE", recurrence=None),
                ],
            )
        ]
        text = self.renderer.render_grouped_list(groups, ReminderListFilter.ALL)
        self.assertIn("[重複] R-REC", text)
        # 一次性提醒不應有前綴
        self.assertIn("- R-ONE", text)
        self.assertNotIn("[重複] R-ONE", text)


class ReminderListKeyboardTest(unittest.TestCase):
    """按鈕文字的 `[重複]` 前綴：跟 render_grouped_list 對稱，
    避免只有訊息本文標示、按鈕沒標示的不一致。"""

    def test_keyboard_button_has_bracket_prefix_for_recurring(self) -> None:
        from remindly.reminders.renderer import reminder_list_keyboard

        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=0,
            month_days=(1, 18, 25),
        )
        groups = [
            ReminderListGroup(
                creator_user_id=7,
                creator_label="你",
                reminders=[
                    _reminder(short_id="R-REC", recurrence=rule),
                    _reminder(id="rmd_2", short_id="R-ONE", recurrence=None),
                ],
            )
        ]
        markup = reminder_list_keyboard(groups, ReminderListFilter.ALL)
        button_texts = [
            button["text"]
            for row in markup["inline_keyboard"]
            for button in row
        ]

        recurring_button = next(t for t in button_texts if "R-REC" in t)
        oneoff_button = next(t for t in button_texts if "R-ONE" in t)

        self.assertTrue(recurring_button.startswith("[重複] "))
        self.assertFalse(oneoff_button.startswith("[重複]"))


if __name__ == "__main__":
    unittest.main()
