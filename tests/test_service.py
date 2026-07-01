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
