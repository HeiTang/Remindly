from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from remindly.reminders.drafts import DraftStore, EditSession, EditSessionStore
from remindly.reminders.models import (
    MentionKind,
    Participant,
    Reminder,
    ReminderDraft,
    ReminderStatus,
)
from remindly.reminders.parser import ReminderParser
from remindly.reminders.service import (
    EXPIRED_DRAFT_TEXT,
    EXPIRED_EDIT_TEXT,
    ReminderListFilter,
    ReminderService,
)
from remindly.storage.session_stores import SqliteDraftStore, SqliteEditSessionStore
from remindly.storage.sqlite import ReminderRepository
from remindly.telegram.models import TelegramChat, TelegramMessage, TelegramUser


class ReminderServiceListGroupingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = ReminderRepository(Path(self.temp_dir.name) / "test.db")
        self.repository.migrate()
        self.zone = ZoneInfo("Asia/Taipei")
        self.now = datetime(2026, 6, 3, 12, 0, tzinfo=self.zone)
        self.service = ReminderService(
            repository=self.repository,
            parser=ReminderParser("Asia/Taipei"),
            draft_store=DraftStore(ttl_minutes=10),
            edit_store=EditSessionStore(ttl_minutes=10),
            default_timezone="Asia/Taipei",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_pending_grouped_by_creator(self) -> None:
        self.repository.create_reminder(
            self._reminder("rmd_1", "R-0001", creator_user_id=7, title="倒垃圾", hours=1),
            [
                Participant(
                    user_id=7,
                    username="orange",
                    display_name="@orange",
                    mention_kind=MentionKind.USERNAME,
                )
            ],
        )
        self.repository.create_reminder(
            self._reminder("rmd_2", "R-0002", creator_user_id=8, title="開會", hours=2),
            [
                Participant(
                    user_id=8,
                    username="alice",
                    display_name="@alice",
                    mention_kind=MentionKind.USERNAME,
                )
            ],
        )

        groups = self.service.list_pending_grouped(chat_id=100, viewer_user_id=7, now=self.now)

        self.assertEqual(["你", "@alice"], [group.creator_label for group in groups])
        self.assertEqual(
            [["R-0001"], ["R-0002"]],
            [[reminder.short_id for reminder in group.reminders] for group in groups],
        )

    def test_list_filter_today(self) -> None:
        self.repository.create_reminder(
            self._reminder("rmd_1", "R-0001", creator_user_id=7, title="倒垃圾", hours=1),
            [],
        )
        self.repository.create_reminder(
            self._reminder("rmd_2", "R-0002", creator_user_id=8, title="明天開會", hours=24),
            [],
        )

        groups = self.service.list_pending_grouped(
            chat_id=100,
            viewer_user_id=7,
            now=self.now,
            list_filter=ReminderListFilter.TODAY,
        )

        self.assertEqual([["R-0001"]], reminders_by_group(groups))

    def test_list_filter_week_excludes_next_week(self) -> None:
        self.repository.create_reminder(
            self._reminder("rmd_1", "R-0001", creator_user_id=7, title="本週", hours=24),
            [],
        )
        self.repository.create_reminder(
            self._reminder("rmd_2", "R-0002", creator_user_id=7, title="下週", hours=24 * 7),
            [],
        )

        groups = self.service.list_pending_grouped(
            chat_id=100,
            viewer_user_id=7,
            now=self.now,
            list_filter=ReminderListFilter.WEEK,
        )

        self.assertEqual([["R-0001"]], reminders_by_group(groups))

    def test_list_filter_mine(self) -> None:
        self.repository.create_reminder(
            self._reminder("rmd_1", "R-0001", creator_user_id=7, title="我的", hours=1),
            [],
        )
        self.repository.create_reminder(
            self._reminder("rmd_2", "R-0002", creator_user_id=8, title="別人的", hours=2),
            [],
        )

        groups = self.service.list_pending_grouped(
            chat_id=100,
            viewer_user_id=7,
            now=self.now,
            list_filter=ReminderListFilter.MINE,
        )

        self.assertEqual([["R-0001"]], reminders_by_group(groups))

    def _reminder(
        self,
        reminder_id: str,
        short_id: str,
        *,
        creator_user_id: int,
        title: str,
        hours: int,
    ) -> Reminder:
        return Reminder(
            id=reminder_id,
            short_id=short_id,
            chat_id=100,
            chat_type="group",
            creator_user_id=creator_user_id,
            title=title,
            remind_at=self.now + timedelta(hours=hours),
            timezone="Asia/Taipei",
            status=ReminderStatus.PENDING,
            source_text=title,
            parse_result={},
            created_at=self.now,
            updated_at=self.now,
        )


def reminders_by_group(groups):
    return [[reminder.short_id for reminder in group.reminders] for group in groups]


class ReminderServiceSnoozeTest(unittest.TestCase):
    """驗證 snooze 按鈕的三種延後語意。

    `1d` 對應按鈕文字「明天 HH:MM」— 保留原提醒時段、日期用 now 的隔天，
    避免「明天同時間」對『同時間』誰為基準的歧義。
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = ReminderRepository(Path(self.temp_dir.name) / "test.db")
        self.repository.migrate()
        self.zone = ZoneInfo("Asia/Taipei")
        self.service = ReminderService(
            repository=self.repository,
            parser=ReminderParser("Asia/Taipei"),
            draft_store=DraftStore(ttl_minutes=10),
            edit_store=EditSessionStore(ttl_minutes=10),
            default_timezone="Asia/Taipei",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _persist_reminder(self, remind_at: datetime) -> Reminder:
        reminder = Reminder(
            id="rmd_snz",
            short_id="R-SNZ1",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="吃藥",
            remind_at=remind_at,
            timezone="Asia/Taipei",
            status=ReminderStatus.FIRED,
            source_text="",
            parse_result={},
            created_at=remind_at - timedelta(minutes=5),
            updated_at=remind_at,
        )
        self.repository.create_reminder(reminder, [])
        return reminder

    def test_snooze_1d_uses_tomorrow_date_and_original_time(self) -> None:
        self._persist_reminder(datetime(2026, 6, 3, 9, 0, tzinfo=self.zone))
        click_at = datetime(2026, 6, 3, 9, 5, tzinfo=self.zone)

        result = self.service.snooze(100, "R-SNZ1", 7, "1d", click_at)

        self.assertIsNotNone(result)
        # 明天 (6/4) 09:00 — 保留原時段
        self.assertEqual(
            datetime(2026, 6, 4, 9, 0, tzinfo=self.zone),
            result.reminder.remind_at,
        )

    def test_snooze_1d_uses_click_date_when_reading_late_same_day(self) -> None:
        """9:00 提醒，下午 3pm 才點『明天 09:00』→ 明天 09:00（不是今天下午）"""
        self._persist_reminder(datetime(2026, 6, 3, 9, 0, tzinfo=self.zone))
        click_at = datetime(2026, 6, 3, 15, 0, tzinfo=self.zone)

        result = self.service.snooze(100, "R-SNZ1", 7, "1d", click_at)

        self.assertEqual(
            datetime(2026, 6, 4, 9, 0, tzinfo=self.zone),
            result.reminder.remind_at,
        )

    def test_snooze_1d_uses_click_date_when_reading_days_late(self) -> None:
        """週日 9:00 錯過，週三下午才點『明天 09:00』→ 週四 09:00（不是週一）"""
        self._persist_reminder(datetime(2026, 6, 7, 9, 0, tzinfo=self.zone))  # 週日
        click_at = datetime(2026, 6, 10, 14, 0, tzinfo=self.zone)  # 週三下午

        result = self.service.snooze(100, "R-SNZ1", 7, "1d", click_at)

        self.assertEqual(
            datetime(2026, 6, 11, 9, 0, tzinfo=self.zone),  # 週四 09:00
            result.reminder.remind_at,
        )

    def test_snooze_1d_uses_click_date_when_reading_before_original_time_of_day(
        self,
    ) -> None:
        """週日 9:00 錯過，週三**上午 8am**（早於 9:00）才點『明天 09:00』→ 週四 09:00"""
        self._persist_reminder(datetime(2026, 6, 7, 9, 0, tzinfo=self.zone))
        click_at = datetime(2026, 6, 10, 8, 0, tzinfo=self.zone)

        result = self.service.snooze(100, "R-SNZ1", 7, "1d", click_at)

        # 不是週三 09:00（1 小時後那個選項才對），而是週四 09:00
        self.assertEqual(
            datetime(2026, 6, 11, 9, 0, tzinfo=self.zone),
            result.reminder.remind_at,
        )

    def test_snooze_10m_is_now_plus_delay(self) -> None:
        self._persist_reminder(datetime(2026, 6, 3, 9, 0, tzinfo=self.zone))
        click_at = datetime(2026, 6, 3, 9, 5, tzinfo=self.zone)

        result = self.service.snooze(100, "R-SNZ1", 7, "10m", click_at)

        self.assertEqual(
            click_at + timedelta(minutes=10),
            result.reminder.remind_at,
        )

    def test_snooze_1h_is_now_plus_delay(self) -> None:
        self._persist_reminder(datetime(2026, 6, 3, 9, 0, tzinfo=self.zone))
        click_at = datetime(2026, 6, 3, 9, 5, tzinfo=self.zone)

        result = self.service.snooze(100, "R-SNZ1", 7, "1h", click_at)

        self.assertEqual(
            click_at + timedelta(hours=1),
            result.reminder.remind_at,
        )


class ReminderServiceRecurringCreateTest(unittest.TestCase):
    """Phase 2 端到端：一句「每個月 1, 18, 25 號 09:00 提醒我繳信用卡」→
    begin_create 應該產生帶 recurrence 的 draft、confirm 應該把 recurrence 帶進 Reminder。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = ReminderRepository(Path(self.temp_dir.name) / "test.db")
        self.repository.migrate()
        self.zone = ZoneInfo("Asia/Taipei")
        self.now = datetime(2026, 7, 4, 8, 0, tzinfo=self.zone)
        self.service = ReminderService(
            repository=self.repository,
            parser=ReminderParser("Asia/Taipei"),
            draft_store=DraftStore(ttl_minutes=10),
            edit_store=EditSessionStore(ttl_minutes=10),
            default_timezone="Asia/Taipei",
        )
        self.message = TelegramMessage(
            id=1,
            chat=TelegramChat(id=100, type="private"),
            from_user=TelegramUser(id=7, first_name="Orange", username="orange"),
            text="",
            entities=(),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_begin_create_populates_draft_recurrence(self) -> None:
        from remindly.reminders.models import RecurrencePeriod
        from remindly.reminders.service import Confirmation

        result = self.service.begin_create(
            "每個月 1, 18, 25 號 09:00 提醒我繳信用卡",
            self.message,
            self.now,
        )
        self.assertIsInstance(result, Confirmation)
        self.assertIsNotNone(result.draft.recurrence)
        self.assertEqual(RecurrencePeriod.MONTHLY, result.draft.recurrence.period)
        self.assertEqual((1, 18, 25), result.draft.recurrence.month_days)
        self.assertEqual("繳信用卡", result.draft.title)

    def test_confirm_transfers_recurrence_to_reminder(self) -> None:
        from remindly.reminders.service import Confirmation

        result = self.service.begin_create(
            "每天 09:00 提醒我吃藥",
            self.message,
            self.now,
        )
        assert isinstance(result, Confirmation)
        draft_id = result.draft.id

        create_result = self.service.confirm(draft_id, 7, self.now)
        self.assertIsNotNone(create_result)
        self.assertIsNotNone(create_result.reminder.recurrence)
        self.assertEqual(result.draft.recurrence, create_result.reminder.recurrence)

        # 從 DB 讀回時也帶著 recurrence
        stored = self.repository.get_by_short_id(100, create_result.reminder.short_id)
        self.assertEqual(create_result.reminder.recurrence, stored.recurrence)


class ReminderServiceRecurringActionsTest(unittest.TestCase):
    """Phase 4a：skip_next_occurrence + cancel_series。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = ReminderRepository(Path(self.temp_dir.name) / "test.db")
        self.repository.migrate()
        self.zone = ZoneInfo("Asia/Taipei")
        self.now = datetime(2026, 7, 4, 8, 0, tzinfo=self.zone)
        self.service = ReminderService(
            repository=self.repository,
            parser=ReminderParser("Asia/Taipei"),
            draft_store=DraftStore(ttl_minutes=10),
            edit_store=EditSessionStore(ttl_minutes=10),
            default_timezone="Asia/Taipei",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _persist_recurring(
        self,
        remind_at: datetime,
        rule=None,
    ) -> Reminder:
        from remindly.reminders.models import RecurrencePeriod, RecurrenceRule

        if rule is None:
            rule = RecurrenceRule(
                period=RecurrencePeriod.MONTHLY,
                hour=9,
                minute=0,
                month_days=(1, 18, 25),
            )
        reminder = Reminder(
            id="rmd_p4a",
            short_id="R-P4A",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="繳信用卡",
            remind_at=remind_at,
            timezone="Asia/Taipei",
            status=ReminderStatus.PENDING,
            source_text="",
            parse_result={},
            created_at=remind_at,
            updated_at=remind_at,
            recurrence=rule,
        )
        self.repository.create_reminder(reminder, [])
        return reminder

    def test_skip_next_advances_one_iteration(self) -> None:
        """每月 1, 18, 25 已排 7/18；skip 一次 → 7/25。"""
        self._persist_recurring(datetime(2026, 7, 18, 9, 0, tzinfo=self.zone))
        result = self.service.skip_next_occurrence(100, "R-P4A", 7, self.now)
        self.assertIsNotNone(result)
        self.assertEqual(
            datetime(2026, 7, 25, 9, 0, tzinfo=self.zone),
            result.reminder.remind_at,
        )

    def test_skip_next_crosses_month_boundary(self) -> None:
        """已排 7/25（本月最後一次）；skip → 8/1。"""
        self._persist_recurring(datetime(2026, 7, 25, 9, 0, tzinfo=self.zone))
        result = self.service.skip_next_occurrence(100, "R-P4A", 7, self.now)
        self.assertEqual(
            datetime(2026, 8, 1, 9, 0, tzinfo=self.zone),
            result.reminder.remind_at,
        )

    def test_skip_next_rejects_non_recurring_reminder(self) -> None:
        """一次性提醒不能 skip_next。"""
        reminder = Reminder(
            id="rmd_once",
            short_id="R-ONE",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="X",
            remind_at=self.now + timedelta(hours=1),
            timezone="Asia/Taipei",
            status=ReminderStatus.PENDING,
            source_text="",
            parse_result={},
            created_at=self.now,
            updated_at=self.now,
        )
        self.repository.create_reminder(reminder, [])
        self.assertIsNone(self.service.skip_next_occurrence(100, "R-ONE", 7, self.now))

    def test_skip_next_rejects_non_creator(self) -> None:
        self._persist_recurring(datetime(2026, 7, 18, 9, 0, tzinfo=self.zone))
        self.assertIsNone(
            self.service.skip_next_occurrence(100, "R-P4A", 999, self.now)
        )

    def test_cancel_series_marks_cancelled(self) -> None:
        self._persist_recurring(datetime(2026, 7, 18, 9, 0, tzinfo=self.zone))
        cancelled = self.service.cancel_series(100, "R-P4A", 7)
        self.assertIsNotNone(cancelled)
        self.assertEqual(ReminderStatus.CANCELLED, cancelled.status)

    def test_cancel_series_rejects_non_creator(self) -> None:
        self._persist_recurring(datetime(2026, 7, 18, 9, 0, tzinfo=self.zone))
        self.assertIsNone(self.service.cancel_series(100, "R-P4A", 999))

    def test_skip_next_returns_none_when_reminder_no_longer_pending(self) -> None:
        """Race with scheduler.claim_due：advance_pending 的 UPDATE guarded on
        status = PENDING；若 select 之後、update 之前 scheduler 把 status 改成
        FIRING，rowcount = 0，服務層應該 return None（別回傳假成功結果）。"""
        self._persist_recurring(datetime(2026, 7, 18, 9, 0, tzinfo=self.zone))
        # 模擬 scheduler 在中間把 status 改到 FIRING
        with self.repository.connect() as connection:
            connection.execute(
                "update reminders set status = ? where id = ?",
                ("firing", "rmd_p4a"),
            )

        self.assertIsNone(
            self.service.skip_next_occurrence(100, "R-P4A", 7, self.now)
        )
        # 原本的 remind_at 沒被更改
        stored = self.repository.get_by_short_id(100, "R-P4A")
        self.assertEqual(
            datetime(2026, 7, 18, 9, 0, tzinfo=self.zone),
            stored.remind_at,
        )


class ReminderServiceSweepExpiredPromptsTest(unittest.TestCase):
    """對 sweep_expired_prompts 做 service + SQLite store 的整合測試，
    scheduler 那邊用的 FakePromptSweeper 抓不到這條路徑上的 regression。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = ReminderRepository(Path(self.temp_dir.name) / "test.db")
        self.repository.migrate()
        self.zone = ZoneInfo("Asia/Taipei")
        self.now = datetime(2026, 6, 3, 12, 0, tzinfo=self.zone)
        self.draft_store = SqliteDraftStore(ttl_minutes=10, repository=self.repository)
        self.edit_store = SqliteEditSessionStore(ttl_minutes=10, repository=self.repository)
        self.service = ReminderService(
            repository=self.repository,
            parser=ReminderParser("Asia/Taipei"),
            draft_store=self.draft_store,
            edit_store=self.edit_store,
            default_timezone="Asia/Taipei",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _draft(self, draft_id: str, *, creator_user_id: int = 7) -> ReminderDraft:
        return ReminderDraft(
            id=draft_id,
            chat_id=100,
            chat_type="private",
            creator_user_id=creator_user_id,
            timezone="Asia/Taipei",
            source_text="提醒我",
            title=None,
            remind_at=None,
            participants=[],
            missing_fields=["title", "time"],
            parse_result={},
        )

    def test_sweep_returns_only_prompts_with_message_id(self) -> None:
        with_msg = self._draft("draft_with_msg", creator_user_id=7)
        self.draft_store.save(with_msg, self.now)
        self.draft_store.set_prompt_message_id("draft_with_msg", 555)

        without_msg = self._draft("draft_without_msg", creator_user_id=8)
        self.draft_store.save(without_msg, self.now)
        # 故意不呼叫 set_prompt_message_id

        later = self.now + timedelta(minutes=11)
        expired = self.service.sweep_expired_prompts(later)

        # 只有帶 prompt_message_id 的 draft 會被回傳
        self.assertEqual(1, len(expired))
        self.assertEqual(555, expired[0].message_id)
        self.assertEqual(100, expired[0].chat_id)
        self.assertEqual(EXPIRED_DRAFT_TEXT, expired[0].text)

    def test_sweep_deletes_all_expired_rows(self) -> None:
        with_msg = self._draft("draft_with_msg", creator_user_id=7)
        self.draft_store.save(with_msg, self.now)
        self.draft_store.set_prompt_message_id("draft_with_msg", 555)

        without_msg = self._draft("draft_without_msg", creator_user_id=8)
        self.draft_store.save(without_msg, self.now)

        later = self.now + timedelta(minutes=11)
        self.service.sweep_expired_prompts(later)

        # 兩筆 row 都應該被刪掉（即使 without_msg 沒被回傳給 caller）
        self.assertIsNone(self.draft_store.get_by_id("draft_with_msg", later))
        self.assertIsNone(self.draft_store.get_by_id("draft_without_msg", later))
        self.assertEqual([], self.draft_store.list_expired(later))

    def test_sweep_preserves_unexpired_drafts(self) -> None:
        fresh = self._draft("draft_fresh")
        self.draft_store.save(fresh, self.now)
        self.draft_store.set_prompt_message_id("draft_fresh", 999)

        still_alive = self.now + timedelta(minutes=5)
        expired = self.service.sweep_expired_prompts(still_alive)

        self.assertEqual([], expired)
        self.assertIsNotNone(self.draft_store.get_by_id("draft_fresh", still_alive))

    def test_sweep_handles_edit_sessions(self) -> None:
        with_msg = EditSession(chat_id=200, user_id=7, short_id="R-M", field="time")
        self.edit_store.save(with_msg, self.now)
        self.edit_store.set_prompt_message_id(200, 7, 777)

        without_msg = EditSession(chat_id=200, user_id=8, short_id="R-N", field="title")
        self.edit_store.save(without_msg, self.now)

        later = self.now + timedelta(minutes=11)
        expired = self.service.sweep_expired_prompts(later)

        self.assertEqual(1, len(expired))
        self.assertEqual(777, expired[0].message_id)
        self.assertEqual(200, expired[0].chat_id)
        self.assertEqual(EXPIRED_EDIT_TEXT, expired[0].text)
        # 兩筆 edit session 都應該被刪掉
        self.assertIsNone(self.edit_store.get_for_context(200, 7, later))
        self.assertIsNone(self.edit_store.get_for_context(200, 8, later))

    def test_sweep_returns_mixed_draft_and_edit_prompts(self) -> None:
        draft = self._draft("draft_mixed", creator_user_id=7)
        self.draft_store.save(draft, self.now)
        self.draft_store.set_prompt_message_id("draft_mixed", 111)

        session = EditSession(chat_id=300, user_id=9, short_id="R-X", field="title")
        self.edit_store.save(session, self.now)
        self.edit_store.set_prompt_message_id(300, 9, 222)

        later = self.now + timedelta(minutes=11)
        expired = self.service.sweep_expired_prompts(later)

        by_text = {prompt.text: prompt for prompt in expired}
        self.assertEqual(2, len(expired))
        self.assertEqual(111, by_text[EXPIRED_DRAFT_TEXT].message_id)
        self.assertEqual(222, by_text[EXPIRED_EDIT_TEXT].message_id)


if __name__ == "__main__":
    unittest.main()
