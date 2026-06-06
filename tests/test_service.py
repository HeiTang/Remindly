from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from remindly.reminders.drafts import DraftStore, EditSessionStore
from remindly.reminders.models import (
    MentionKind,
    Participant,
    Reminder,
    ReminderStatus,
)
from remindly.reminders.parser import ReminderParser
from remindly.reminders.service import ReminderListFilter, ReminderService
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


if __name__ == "__main__":
    unittest.main()
