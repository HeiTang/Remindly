from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from remindly.reminders.models import (
    MentionKind,
    Participant,
    Reminder,
    ReminderStatus,
)
from remindly.storage.sqlite import ReminderRepository


class ReminderRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test.db"
        self.repository = ReminderRepository(self.database_path)
        self.repository.migrate()
        self.zone = ZoneInfo("Asia/Taipei")
        self.now = datetime(2026, 6, 3, 12, 0, tzinfo=self.zone)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_list_cancel(self) -> None:
        reminder = self._reminder(remind_at=self.now + timedelta(hours=1))
        self.repository.create_reminder(
            reminder,
            [
                Participant(
                    display_name="@orange",
                    mention_kind=MentionKind.USERNAME,
                    username="orange",
                )
            ],
        )

        pending = self.repository.list_pending(chat_id=100)
        self.assertEqual(1, len(pending))
        self.assertEqual("倒垃圾", pending[0].title)

        cancelled = self.repository.cancel(chat_id=100, short_id="R-TEST", actor_user_id=7)
        self.assertIsNotNone(cancelled)
        self.assertEqual([], self.repository.list_pending(chat_id=100))

    def test_update_title_and_time(self) -> None:
        reminder = self._reminder(remind_at=self.now + timedelta(hours=1))
        self.repository.create_reminder(reminder, [])

        new_time = self.now + timedelta(days=1)
        updated_title = self.repository.update_title(
            chat_id=100,
            short_id="R-TEST",
            actor_user_id=7,
            title="開會",
            now=self.now,
        )
        updated_time = self.repository.update_remind_at(
            chat_id=100,
            short_id="R-TEST",
            actor_user_id=7,
            remind_at=new_time,
            now=self.now,
        )

        self.assertIsNotNone(updated_title)
        self.assertIsNotNone(updated_time)
        pending = self.repository.list_pending(chat_id=100)
        self.assertEqual("開會", pending[0].title)
        self.assertEqual(new_time, pending[0].remind_at)

    def test_update_rejects_non_creator(self) -> None:
        reminder = self._reminder(remind_at=self.now + timedelta(hours=1))
        self.repository.create_reminder(reminder, [])

        updated = self.repository.update_title(
            chat_id=100,
            short_id="R-TEST",
            actor_user_id=999,
            title="開會",
            now=self.now,
        )

        self.assertIsNone(updated)
        self.assertEqual("倒垃圾", self.repository.list_pending(chat_id=100)[0].title)

    def test_claim_due_is_idempotent(self) -> None:
        reminder = self._reminder(remind_at=self.now - timedelta(minutes=1))
        self.repository.create_reminder(reminder, [])

        first_claim = self.repository.claim_due(self.now)
        second_claim = self.repository.claim_due(self.now)

        self.assertEqual(1, len(first_claim))
        self.assertEqual([], second_claim)

    def test_snooze_fired_reminder_requeues_pending(self) -> None:
        reminder = self._reminder(remind_at=self.now - timedelta(minutes=1))
        self.repository.create_reminder(reminder, [])
        claimed = self.repository.claim_due(self.now)
        self.repository.mark_fired(claimed[0].id, self.now)

        snoozed = self.repository.snooze(
            chat_id=100,
            short_id="R-TEST",
            actor_user_id=7,
            remind_at=self.now + timedelta(minutes=10),
            now=self.now,
        )

        self.assertIsNotNone(snoozed)
        pending = self.repository.list_pending(chat_id=100)
        self.assertEqual(1, len(pending))
        self.assertEqual(self.now + timedelta(minutes=10), pending[0].remind_at)

    def test_snooze_rejects_non_creator(self) -> None:
        reminder = self._reminder(remind_at=self.now - timedelta(minutes=1))
        self.repository.create_reminder(reminder, [])
        claimed = self.repository.claim_due(self.now)
        self.repository.mark_fired(claimed[0].id, self.now)

        snoozed = self.repository.snooze(
            chat_id=100,
            short_id="R-TEST",
            actor_user_id=999,
            remind_at=self.now + timedelta(minutes=10),
            now=self.now,
        )

        self.assertIsNone(snoozed)
        self.assertEqual([], self.repository.list_pending(chat_id=100))

    def test_chat_natural_language_setting_defaults_to_disabled(self) -> None:
        self.assertFalse(self.repository.is_chat_natural_language_enabled(100))

        self.repository.set_chat_natural_language_enabled(100, True, 7, self.now)

        self.assertTrue(self.repository.is_chat_natural_language_enabled(100))

        self.repository.set_chat_natural_language_enabled(100, False, 7, self.now)

        self.assertFalse(self.repository.is_chat_natural_language_enabled(100))

    def test_mark_fired_does_not_override_snoozed_firing_reminder(self) -> None:
        reminder = self._reminder(remind_at=self.now - timedelta(minutes=1))
        self.repository.create_reminder(reminder, [])
        claimed = self.repository.claim_due(self.now)

        self.repository.snooze(
            chat_id=100,
            short_id="R-TEST",
            actor_user_id=7,
            remind_at=self.now + timedelta(minutes=10),
            now=self.now,
        )
        self.repository.mark_fired(claimed[0].id, self.now)

        pending = self.repository.list_pending(chat_id=100)
        self.assertEqual(1, len(pending))
        self.assertEqual(ReminderStatus.PENDING, pending[0].status)

    def _reminder(self, remind_at: datetime) -> Reminder:
        return Reminder(
            id="rmd_test",
            short_id="R-TEST",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="倒垃圾",
            remind_at=remind_at,
            timezone="Asia/Taipei",
            status=ReminderStatus.PENDING,
            source_text="明天下午三點提醒我倒垃圾",
            parse_result={},
            created_at=self.now,
            updated_at=self.now,
        )


if __name__ == "__main__":
    unittest.main()
